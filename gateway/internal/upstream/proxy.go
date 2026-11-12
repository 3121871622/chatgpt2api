// Package upstream provides a TLS-fingerprint-aware HTTP proxy for upstream
// ChatGPT requests. Python owns all business logic (auth, account selection,
// requirements tokens). This package owns only the network I/O layer:
//
//  1. POST /v1/upstream/stream  — receive URL+headers+body from Python, send
//     to upstream with a Chrome110 TLS fingerprint, stream SSE back.
//  2. POST /v1/upstream/poll    — poll a conversation document for image
//     file_ids, return them to Python as JSON.
package upstream

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	fhttp "github.com/bogdanfinn/fhttp"
	tlsclient "github.com/bogdanfinn/tls-client"
	"github.com/bogdanfinn/tls-client/profiles"
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

// StreamHandler handles POST /v1/upstream/stream.
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

	for key, vals := range resp.Header {
		for _, v := range vals {
			w.Header().Add(key, v)
		}
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
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

	client, err := clientForProfile("chrome110", req.TimeoutMs/1000+5)
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

		body, readErr := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
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
	seen := map[string]bool{}
	for _, raw := range msgs {
		var node struct {
			Message *struct {
				Content *struct {
					ContentType string            `json:"content_type"`
					Parts       []json.RawMessage `json:"parts"`
				} `json:"content"`
			} `json:"message"`
		}
		if err := json.Unmarshal(raw, &node); err != nil || node.Message == nil {
			continue
		}
		ct := node.Message.Content
		if ct == nil {
			continue
		}
		for _, rawPart := range ct.Parts {
			var part struct {
				ContentType  string   `json:"content_type"`
				AssetPointer string   `json:"asset_pointer"`
				FileIDs      []string `json:"file_ids"`
				SedimentIDs  []string `json:"sediment_ids"`
			}
			if err := json.Unmarshal(rawPart, &part); err != nil {
				continue
			}
			if part.AssetPointer != "" && !seen[part.AssetPointer] {
				seen[part.AssetPointer] = true
				if strings.HasPrefix(part.AssetPointer, "file-service://") {
					id := strings.TrimPrefix(part.AssetPointer, "file-service://")
					fileIDs = append(fileIDs, id)
				}
			}
			for _, id := range part.FileIDs {
				if !seen[id] {
					seen[id] = true
					fileIDs = append(fileIDs, id)
				}
			}
			for _, id := range part.SedimentIDs {
				if !seen[id] {
					seen[id] = true
					sedimentIDs = append(sedimentIDs, id)
				}
			}
		}
	}
	return
}
