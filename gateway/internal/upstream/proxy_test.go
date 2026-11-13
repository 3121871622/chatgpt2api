package upstream

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// echoSSEServer serves a few SSE lines then closes.
func echoSSEServer() *httptest.Server {
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		for i := 0; i < 3; i++ {
			_, _ = w.Write([]byte("data: {\"line\":" + string(rune('0'+i)) + "}\n\n"))
		}
	}))
}

// convServerWithIDs serves a conversation document containing the given IDs.
func convServerWithIDs(fileService []string, sediment []string, rawFileIDs []string) *httptest.Server {
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var parts []map[string]any
		for _, id := range fileService {
			parts = append(parts, map[string]any{
				"content_type": "image_asset_pointer",
				"asset_pointer": "file-service://" + id,
			})
		}
		for _, id := range sediment {
			parts = append(parts, map[string]any{
				"content_type": "image_asset_pointer",
				"asset_pointer": "sediment://" + id,
			})
		}
		for _, id := range rawFileIDs {
			parts = append(parts, map[string]any{
				"content_type": "image_asset_pointer",
				"asset_pointer": id,
			})
		}
		doc := map[string]any{
			"mapping": map[string]any{
				"msg1": map[string]any{
					"message": map[string]any{
						"author":  map[string]any{"role": "tool"},
						"content": map[string]any{"content_type": "multimodal_text", "parts": parts},
					},
				},
			},
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(doc)
	}))
}

// --- StreamHandler tests ---

func TestStreamHandler_ProxiesSSE(t *testing.T) {
	up := echoSSEServer()
	defer up.Close()
	body := StreamRequest{URL: up.URL, Method: "GET", TimeoutMs: 5_000}
	b, _ := json.Marshal(body)
	req := httptest.NewRequest("POST", "/v1/upstream/stream", strings.NewReader(string(b)))
	rec := httptest.NewRecorder()
	StreamHandler(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d body=%s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), "data:") {
		t.Fatalf("expected SSE data lines, got: %q", rec.Body.String()[:min200(rec.Body.String())])
	}
}

func TestStreamHandler_ExposesUpstreamStatus(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(429)
		_, _ = w.Write([]byte("data: rate-limited\n\n"))
	}))
	defer srv.Close()
	body := StreamRequest{URL: srv.URL, Method: "GET", TimeoutMs: 5_000}
	b, _ := json.Marshal(body)
	req := httptest.NewRequest("POST", "/v1/upstream/stream", strings.NewReader(string(b)))
	rec := httptest.NewRecorder()
	StreamHandler(rec, req)
	// Our wrapper always returns 200, but X-Upstream-Status must carry the real code.
	got := rec.Header().Get("X-Upstream-Status")
	if got != "429" {
		t.Fatalf("want X-Upstream-Status=429, got %q", got)
	}
}

func TestStreamHandler_DropsContentEncoding(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		w.Header().Set("Content-Encoding", "gzip") // tls-client decompresses; we must NOT forward this
		_, _ = w.Write([]byte("data: ok\n\n"))
	}))
	defer srv.Close()
	body := StreamRequest{URL: srv.URL, Method: "GET", TimeoutMs: 5_000}
	b, _ := json.Marshal(body)
	req := httptest.NewRequest("POST", "/v1/upstream/stream", strings.NewReader(string(b)))
	rec := httptest.NewRecorder()
	StreamHandler(rec, req)
	if ce := rec.Header().Get("Content-Encoding"); ce != "" {
		t.Fatalf("Content-Encoding must be stripped, got %q", ce)
	}
}

func TestStreamHandler_BadRequest(t *testing.T) {
	req := httptest.NewRequest("POST", "/v1/upstream/stream", strings.NewReader("{bad"))
	rec := httptest.NewRecorder()
	StreamHandler(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("want 400, got %d", rec.Code)
	}
}

// --- PollHandler tests ---

func TestPollHandler_FileServiceIDs(t *testing.T) {
	srv := convServerWithIDs([]string{"file_abc123", "file_def456"}, nil, nil)
	defer srv.Close()
	body := PollRequest{URL: srv.URL, TimeoutMs: 5_000, IntervalMs: 100, MaxAttempts: 10}
	b, _ := json.Marshal(body)
	req := httptest.NewRequest("POST", "/v1/upstream/poll", strings.NewReader(string(b)))
	rec := httptest.NewRecorder()
	PollHandler(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d body=%s", rec.Code, rec.Body.String())
	}
	var resp PollResponse
	_ = json.Unmarshal(rec.Body.Bytes(), &resp)
	if len(resp.FileIDs) != 2 || resp.FileIDs[0] != "file_abc123" {
		t.Fatalf("unexpected file_ids: %v", resp.FileIDs)
	}
}

func TestPollHandler_SedimentIDs(t *testing.T) {
	srv := convServerWithIDs(nil, []string{"sed_xyz999"}, nil)
	defer srv.Close()
	body := PollRequest{URL: srv.URL, TimeoutMs: 5_000, IntervalMs: 100, MaxAttempts: 10}
	b, _ := json.Marshal(body)
	req := httptest.NewRequest("POST", "/v1/upstream/poll", strings.NewReader(string(b)))
	rec := httptest.NewRecorder()
	PollHandler(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d", rec.Code)
	}
	var resp PollResponse
	_ = json.Unmarshal(rec.Body.Bytes(), &resp)
	if len(resp.SedimentIDs) != 1 || resp.SedimentIDs[0] != "sed_xyz999" {
		t.Fatalf("unexpected sediment_ids: %v", resp.SedimentIDs)
	}
}

func TestPollHandler_Timeout(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"mapping":{}}`))
	}))
	defer srv.Close()
	body := PollRequest{URL: srv.URL, TimeoutMs: 300, IntervalMs: 50, MaxAttempts: 3}
	b, _ := json.Marshal(body)
	req := httptest.NewRequest("POST", "/v1/upstream/poll", strings.NewReader(string(b)))
	rec := httptest.NewRecorder()
	start := time.Now()
	PollHandler(rec, req)
	if rec.Code != http.StatusGatewayTimeout {
		t.Fatalf("want 504, got %d", rec.Code)
	}
	if time.Since(start) > 5*time.Second {
		t.Fatalf("poll took too long")
	}
}

// --- extractImageIDs tests ---

func TestExtractImageIDs_FileServiceAssetPointer(t *testing.T) {
	doc := `{"mapping":{"m1":{"message":{"content":{"parts":[{"content_type":"image_asset_pointer","asset_pointer":"file-service://file_XYZ789"}]}}}}}`
	ids, seds := extractImageIDs([]byte(doc))
	if len(ids) != 1 || ids[0] != "file_XYZ789" {
		t.Fatalf("got ids=%v seds=%v", ids, seds)
	}
}

func TestExtractImageIDs_SedimentAssetPointer(t *testing.T) {
	doc := `{"mapping":{"m1":{"message":{"content":{"parts":[{"content_type":"image_asset_pointer","asset_pointer":"sediment://sed_ABCDEF"}]}}}}}`
	_, seds := extractImageIDs([]byte(doc))
	if len(seds) != 1 || seds[0] != "sed_ABCDEF" {
		t.Fatalf("got seds=%v", seds)
	}
}

func TestExtractImageIDs_RawFileID(t *testing.T) {
	// file_00000000 + 24 hex chars = 32 total chars after file_
	rawID := "file_00000000aabbccddeeff001122334455"
	doc := `{"mapping":{"m1":{"message":{"content":{"parts":[{"content_type":"image_asset_pointer","asset_pointer":"` + rawID + `"}]}}}}}`
	ids, _ := extractImageIDs([]byte(doc))
	if len(ids) != 1 || ids[0] != rawID {
		t.Fatalf("got ids=%v", ids)
	}
}

func TestExtractImageIDs_InMetadata(t *testing.T) {
	// ID buried in metadata, not in parts — Python walks the entire message object
	doc := `{"mapping":{"m1":{"message":{"metadata":{"dalle":{"asset_pointer":"file-service://meta_id_123"}}}}}}`
	ids, _ := extractImageIDs([]byte(doc))
	if len(ids) != 1 || ids[0] != "meta_id_123" {
		t.Fatalf("expected meta_id_123, got %v", ids)
	}
}

func TestExtractImageIDs_ExplicitListsInParts(t *testing.T) {
	// file_ids / sediment_ids stored as plain strings with file-service:// and
	// sediment:// prefixes inside a nested string value — verifies the walker
	// recurses into array values and applies all three regexes.
	doc := `{"mapping":{"m1":{"message":{"content":{"parts":[{"note":"file-service://fsid1 and sediment://sed1"}]}}}}}`
	ids, seds := extractImageIDs([]byte(doc))
	if len(ids) != 1 || ids[0] != "fsid1" {
		t.Fatalf("ids=%v", ids)
	}
	if len(seds) != 1 || seds[0] != "sed1" {
		t.Fatalf("seds=%v", seds)
	}
}

func TestExtractImageIDs_NoDuplicates(t *testing.T) {
	// Same pointer appears in two different messages
	doc := `{"mapping":{
		"m1":{"message":{"content":{"parts":[{"asset_pointer":"file-service://dup_id"}]}}},
		"m2":{"message":{"content":{"parts":[{"asset_pointer":"file-service://dup_id"}]}}}
	}}`
	ids, _ := extractImageIDs([]byte(doc))
	if len(ids) != 1 {
		t.Fatalf("expected 1 deduplicated id, got %v", ids)
	}
}

func TestExtractImageIDs_EmptyDocument(t *testing.T) {
	ids, seds := extractImageIDs([]byte(`{}`))
	if len(ids) != 0 || len(seds) != 0 {
		t.Fatalf("expected empty, got %v %v", ids, seds)
	}
}

func TestExtractImageIDs_MalformedJSON(t *testing.T) {
	ids, seds := extractImageIDs([]byte(`not json`))
	if len(ids) != 0 || len(seds) != 0 {
		t.Fatalf("expected empty on bad json, got %v %v", ids, seds)
	}
}

// Compile-time interface checks.
var _ http.HandlerFunc = http.HandlerFunc(StreamHandler)
var _ http.HandlerFunc = http.HandlerFunc(PollHandler)

func min200(s string) int {
	if len(s) < 200 {
		return len(s)
	}
	return 200
}

var _ = time.Second
