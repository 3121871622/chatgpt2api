package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/3121871622/chatgpt2api/gateway/internal/scheduler"
)

const testToken = "test-token-0123456789"

func newTestMux() http.Handler {
	s := &server{ix: scheduler.NewIndex(32), token: testToken}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /v1/stats", s.auth(func(w http.ResponseWriter, _ *http.Request) { reply(w, s.ix.Stats()) }))
	mux.HandleFunc("POST /v1/accounts/upsert", s.auth(s.upsert))
	mux.HandleFunc("POST /v1/accounts/remove", s.auth(s.remove))
	mux.HandleFunc("POST /v1/lease/acquire", s.auth(s.acquire))
	mux.HandleFunc("POST /v1/lease/release", s.auth(s.release))
	return mux
}

func call(h http.Handler, method, path, token, body string) *httptest.ResponseRecorder {
	req := httptest.NewRequest(method, path, strings.NewReader(body))
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec
}

func TestRejectsMissingOrWrongToken(t *testing.T) {
	h := newTestMux()
	if rec := call(h, "GET", "/v1/stats", "", ""); rec.Code != http.StatusUnauthorized {
		t.Fatalf("missing token: want 401, got %d", rec.Code)
	}
	if rec := call(h, "GET", "/v1/stats", "wrong-token-value-xx", ""); rec.Code != http.StatusUnauthorized {
		t.Fatalf("wrong token: want 401, got %d", rec.Code)
	}
	if rec := call(h, "GET", "/v1/stats", testToken, ""); rec.Code != http.StatusOK {
		t.Fatalf("valid token: want 200, got %d", rec.Code)
	}
}

func TestUpsertAcquireReleaseRoundTrip(t *testing.T) {
	h := newTestMux()
	body := `{"token":"acct-1","plan":"Free","source":"Web","ready":true}`
	if rec := call(h, "POST", "/v1/accounts/upsert", testToken, body); rec.Code != http.StatusOK {
		t.Fatalf("upsert: got %d", rec.Code)
	}

	var got struct {
		OK    bool   `json:"ok"`
		Token string `json:"token"`
	}
	rec := call(h, "POST", "/v1/lease/acquire", testToken, `{"plans":["free"],"limit":1}`)
	if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil || !got.OK || got.Token != "acct-1" {
		t.Fatalf("acquire: want acct-1, got %+v err=%v body=%s", got, err, rec.Body.String())
	}

	rec = call(h, "POST", "/v1/lease/acquire", testToken, `{"limit":1}`)
	_ = json.Unmarshal(rec.Body.Bytes(), &got)
	if got.OK {
		t.Fatal("second acquire must fail while the only account is leased")
	}

	call(h, "POST", "/v1/lease/release", testToken, `{"token":"acct-1","limit":1}`)
	rec = call(h, "POST", "/v1/lease/acquire", testToken, `{"limit":1}`)
	got.OK, got.Token = false, ""
	_ = json.Unmarshal(rec.Body.Bytes(), &got)
	if !got.OK || got.Token != "acct-1" {
		t.Fatalf("released account should be leasable again, got %+v", got)
	}
}

func TestPlanFilterIsCaseInsensitive(t *testing.T) {
	h := newTestMux()
	call(h, "POST", "/v1/accounts/upsert", testToken, `{"token":"p","plan":"PLUS","source":"web","ready":true}`)
	rec := call(h, "POST", "/v1/lease/acquire", testToken, `{"plans":["Plus"],"limit":1}`)
	if !strings.Contains(rec.Body.String(), `"ok":true`) {
		t.Fatalf("mixed-case plan filter should match, body=%s", rec.Body.String())
	}
}

func TestBadJSONIsRejected(t *testing.T) {
	h := newTestMux()
	if rec := call(h, "POST", "/v1/lease/acquire", testToken, `{not json`); rec.Code != http.StatusBadRequest {
		t.Fatalf("bad json: want 400, got %d", rec.Code)
	}
}
