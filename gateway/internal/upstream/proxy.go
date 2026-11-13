// Package upstream provides a TLS-fingerprint-aware HTTP proxy for upstream
// ChatGPT requests. Python owns all business logic (auth, account selection,
// requirements tokens). This package owns only the network I/O layer:
//
//  1. POST /v1/upstream/stream  — receive URL+headers+body from Python, send
//     to upstream with a Chrome TLS fingerprint, stream SSE lines back.
//     The upstream HTTP status is passed to Python via X-Upstream-Status so
//     it can run ensure_ok() before consuming the stream.
//  2. POST /v1/upstream/poll    — poll a conversation document for image
//     file_ids, return them to Python as JSON.
//
// ID extraction mirrors the three Python regexes:
//
//	FILE_SERVICE_ID_RE  = file-service://([A-Za-z0-9_-]+)
//	REAL_IMAGE_FILE_ID_RE = \bfile_00000000[a-f0-9]{24}\b
//	SEDIMENT_ID_RE      = sediment://([A-Za-z0-9_-]+)
//
// The document walker visits every string value in the conversation mapping,
// matching the Python _extract_image_reference_ids recursive walk.
package upstream

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"regexp"
	"strings"
	"time"

	fhttp "github.com/bogdanfinn/fhttp"
	tlsclient "github.com/bogdanfinn/tls-client"
	"github.com/bogdanfinn/tls-client/profiles"
)

// Python-equivalent regexes for image ID extraction.
var (
	fileServiceRE    = regexp.MustCompile(`file-service://([A-Za-z0-9_\-]+)`)
	realImageFileRE  = regexp.MustCompile(`\bfile_00000000[a-f0-9]{24}\b`)
	sedimentRE       = regexp.MustCompile(`sediment://([A-Za-z0-9_\-]+)`)
)

// StreamRequest is the JSON body Python sends to /v1/upstream/stream.
type StreamRequest struct {
	URL         string            `json:"url"`
	Method      string            `json:"method"`
	Headers     map[string]string `json:"headers"`
	Body        json.RawMessage   `json:"body"`
	Impersonate string            `json:"impersonate"`
	TimeoutMs   int               `json:"timeout_ms"`
}

// PollRequest is the JSON body Python sends to /v1/upstream/poll.
type PollRequest struct {
	URL         string            `json:"url"`
	Headers     map[string]string `json:"headers"`
	Impersonate string            `json:"impersonate"`
	TimeoutMs   int               `json:"timeout_ms"`
	IntervalMs  int               `json:"interval_ms"`
	MaxAttempts int               `json:"max_attempts"`
}

// PollResponse is returned when image file_ids are found.
type PollResponse struct {
	FileIDs     []string `json:"file_ids"`
	SedimentIDs []string `json:"sediment_ids"`
	Attempts    int      `json:"attempts"`
	ElapsedMs   int64    `json:"elapsed_ms"`
}

func clientForProfile(impersonate string, timeoutSecs int) (tlsclient.HttpClient, error) {
	key := strings.ToLower(strings.TrimSpace(impersonate))
	profile, ok := profiles.MappedTLSClients[key]
	if !ok {
		aliases := map[string]string{
			"chrome110": "chrome_110",
			"chrome112": "chrome_112",
			"chrome120": "chrome_120",
			"chrome124": "chrome_124",
			"safari16":  "safari_16_0",
			"edge101":   "chrome_110",
		}
		if mapped, found := aliases[key]; found {
			profile, ok = profiles.MappedTLSClients[mapped]
		}
	}
	if !ok {
		profile = profiles.Chrome_110
	}
	opts := []tlsclient.HttpClientOption{
		tlsclient.WithTimeoutSeconds(timeoutSecs),
		tlsclient.WithClientProfile(profile),
		tlsclient.WithNotFollowRedirects(),
		tlsclient.WithInsecureSkipVerify(),
	}
	return tlsclient.NewHttpClient(tlsclient.NewNoopLogger(), opts...)
}

// hopByHopHeaders are removed before forwarding upstream response headers to
// the caller.  tls-client already decompresses bodies, so forwarding
// Content-Encoding would cause the caller to try decompressing again.
var hopByHopHeaders = map[string]bool{
	"Content-Encoding":    true,
	"Transfer-Encoding":   true,
	"Connection":          true,
	"Keep-Alive":          true,
	"Proxy-Authenticate":  true,
	"Proxy-Authorization": true,
	"Te":                  true,
	"Trailers":            true,
	"Upgrade":             true,
}

// StreamHandler handles POST /v1/upstream/stream.
// It proxies the request with a TLS fingerprint and streams SSE back.
// The upstream HTTP status code is forwarded in X-Upstream-Status so the
// Python caller can distinguish 200 from error responses before reading lines.
func StreamHandler(w http.ResponseWriter, r *http.Request) {
	var req StreamRequest
	if err := json.NewDecoder(io.LimitReader(r.Body, 4<<20)).Decode(&req); err != nil {
		http.Error(w, "bad request: "+err.Error(), http.StatusBadRequest)
		return
	}
	if req.URL == "" {
		http.Error(w, "url is required", http.StatusBadRequest)
		return
	}
	if req.TimeoutMs <= 0 {
		req.TimeoutMs = 300_000
	}
	if req.Impersonate == "" {
		req.Impersonate = "chrome110"
	}
	if req.Method == "" {
		req.Method = "POST"
	}
	timeoutSecs := req.TimeoutMs / 1000

	client, err := clientForProfile(req.Impersonate, timeoutSecs)
	if err != nil {
		http.Error(w, "tls-client init: "+err.Error(), http.StatusInternalServerError)
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), time.Duration(req.TimeoutMs)*time.Millisecond)
	defer cancel()

	var bodyReader io.Reader
	if len(req.Body) > 0 && string(req.Body) != "null" {
		bodyReader = bytes.NewReader(req.Body)
	}

	upReq, err := fhttp.NewRequestWithContext(ctx, req.Method, req.URL, bodyReader)
	if err != nil {
		http.Error(w, "build request: "+err.Error(), http.StatusBadRequest)
		return
	}
	for k, v := range req.Headers {
		upReq.Header.Set(k, v)
	}

	resp, err := client.Do(upReq)
	if err != nil {
		http.Error(w, "upstream error: "+err.Error(), http.StatusBadGateway)
		return
	}
	defer resp.Body.Close()

	// Forward upstream headers, excluding hop-by-hop and Content-Encoding
	// (tls-client already decoded the body; re-forwarding Content-Encoding
	// would cause the Python caller's requests library to decode again).
	for key, vals := range resp.Header {
		if hopByHopHeaders[key] {
			continue
		}
		for _, v := range vals {
			w.Header().Add(key, v)
		}
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	// X-Upstream-Status lets Python run ensure_ok() before reading lines.
	w.Header().Set("X-Upstream-Status", fmt.Sprintf("%d", resp.StatusCode))
	w.WriteHeader(http.StatusOK)

	flusher, canFlush := w.(http.Flusher)
	scanner := bufio.NewScanner(resp.Body)
	scanner.Buffer(make([]byte, 64*1024), 64*1024)
	for scanner.Scan() {
		line := scanner.Bytes()
		_, _ = w.Write(line)
		_, _ = w.Write([]byte("\n"))
		if canFlush {
			flusher.Flush()
		}
	}
}

// PollHandler handles POST /v1/upstream/poll.
// It GETs the conversation document repeatedly until image file_ids appear.
func PollHandler(w http.ResponseWriter, r *http.Request) {
	var req PollRequest
	if err := json.NewDecoder(io.LimitReader(r.Body, 1<<20)).Decode(&req); err != nil {
		http.Error(w, "bad request: "+err.Error(), http.StatusBadRequest)
		return
	}
	if req.URL == "" {
		http.Error(w, "url is required", http.StatusBadRequest)
		return
	}
	if req.TimeoutMs <= 0 {
		req.TimeoutMs = 90_000
	}
	if req.IntervalMs <= 0 {
		req.IntervalMs = 2_000
	}
	if req.MaxAttempts <= 0 {
		req.MaxAttempts = 50
	}
	if req.Impersonate == "" {
		req.Impersonate = "chrome110"
	}

	client, err := clientForProfile(req.Impersonate, req.TimeoutMs/1000+5)
	if err != nil {
		http.Error(w, "tls-client init: "+err.Error(), http.StatusInternalServerError)
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), time.Duration(req.TimeoutMs)*time.Millisecond)
	defer cancel()

	started := time.Now()
	for attempt := 1; attempt <= req.MaxAttempts; attempt++ {
		upReq, err := fhttp.NewRequestWithContext(ctx, "GET", req.URL, nil)
		if err != nil {
			http.Error(w, "build request: "+err.Error(), http.StatusBadRequest)
			return
		}
		for k, v := range req.Headers {
			upReq.Header.Set(k, v)
		}

		resp, err := client.Do(upReq)
		if err != nil {
			if ctx.Err() != nil {
				break
			}
			select {
			case <-ctx.Done():
			case <-time.After(time.Duration(req.IntervalMs) * time.Millisecond):
			}
			continue
		}

		body, readErr := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
		resp.Body.Close()
		if readErr != nil || resp.StatusCode != 200 {
			select {
			case <-ctx.Done():
			case <-time.After(time.Duration(req.IntervalMs) * time.Millisecond):
			}
			continue
		}

		fileIDs, sedIDs := extractImageIDs(body)
		if len(fileIDs) > 0 || len(sedIDs) > 0 {
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(PollResponse{
				FileIDs:     fileIDs,
				SedimentIDs: sedIDs,
				Attempts:    attempt,
				ElapsedMs:   time.Since(started).Milliseconds(),
			})
			return
		}

		select {
		case <-ctx.Done():
		case <-time.After(time.Duration(req.IntervalMs) * time.Millisecond):
		}
	}

	http.Error(w, `{"error":"poll_timeout"}`, http.StatusGatewayTimeout)
}

// addUnique appends s to *out only when it is not already present.
func addUnique(out *[]string, seen map[string]bool, s string) {
	if s != "" && !seen[s] {
		seen[s] = true
		*out = append(*out, s)
	}
}

// extractImageIDs mirrors Python's _extract_image_reference_ids + the
// asset_pointer walk.  It applies three regexes to every string value found
// anywhere in the conversation mapping:
//
//   - file-service://ID     → fileIDs
//   - file_00000000<24hex>  → fileIDs  (raw file ID, no prefix)
//   - sediment://ID         → sedimentIDs
//
// This deliberately matches what Python does: a full recursive walk of the
// entire message object (content, metadata, parts, etc.) searching for any
// string that matches the patterns, not just specific structural fields.
func extractImageIDs(body []byte) (fileIDs []string, sedimentIDs []string) {
	var doc map[string]json.RawMessage
	if err := json.Unmarshal(body, &doc); err != nil {
		return
	}
	mapping, ok := doc["mapping"]
	if !ok {
		return
	}
	var msgs map[string]json.RawMessage
	if err := json.Unmarshal(mapping, &msgs); err != nil {
		return
	}

	seenFile := map[string]bool{}
	seenSed := map[string]bool{}

	for _, raw := range msgs {
		walkJSON(raw, func(s string) {
			// file-service://ID
			for _, m := range fileServiceRE.FindAllStringSubmatch(s, -1) {
				addUnique(&fileIDs, seenFile, m[1])
			}
			// raw file ID: file_00000000<24 hex chars>
			for _, m := range realImageFileRE.FindAllString(s, -1) {
				addUnique(&fileIDs, seenFile, m)
			}
			// sediment://ID
			for _, m := range sedimentRE.FindAllStringSubmatch(s, -1) {
				addUnique(&sedimentIDs, seenSed, m[1])
			}
		})
	}
	return
}

// walkJSON recursively visits every JSON string value in the raw message,
// calling fn for each one.  This replicates Python's recursive walk that
// handles dicts and lists at any nesting depth.
func walkJSON(raw json.RawMessage, fn func(string)) {
	if len(raw) == 0 {
		return
	}
	switch raw[0] {
	case '"':
		var s string
		if json.Unmarshal(raw, &s) == nil {
			fn(s)
		}
	case '{':
		var obj map[string]json.RawMessage
		if json.Unmarshal(raw, &obj) == nil {
			for _, v := range obj {
				walkJSON(v, fn)
			}
		}
	case '[':
		var arr []json.RawMessage
		if json.Unmarshal(raw, &arr) == nil {
			for _, v := range arr {
				walkJSON(v, fn)
			}
		}
	}
}
