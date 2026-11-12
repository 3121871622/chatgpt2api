// Command gateway serves the account lease scheduler over a local HTTP API.
//
// Environment variables:
//
//	GATEWAY_TOKEN   required, ≥16 chars — shared secret for all API calls
//	GATEWAY_ADDR    optional, default "127.0.0.1:8081"
//	DATABASE_URL    optional — PostgreSQL DSN; when set accounts are persisted
//	                to PG and loaded into the in-memory index on startup
package main

import (
	"context"
	"crypto/subtle"
	"encoding/json"
	"log"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/3121871622/chatgpt2api/gateway/internal/scheduler"
	"github.com/3121871622/chatgpt2api/gateway/internal/store"
)

type server struct {
	ix    *scheduler.Index
	st    *store.Store // nil when DATABASE_URL is not set
	token string
}

// --- helpers -----------------------------------------------------------------

func set(items []string) map[string]struct{} {
	if len(items) == 0 {
		return nil
	}
	m := make(map[string]struct{}, len(items))
	for _, v := range items {
		m[strings.ToLower(strings.TrimSpace(v))] = struct{}{}
	}
	return m
}

func (s *server) auth(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		got := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
		if subtle.ConstantTimeCompare([]byte(got), []byte(s.token)) != 1 {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		next(w, r)
	}
}

func decode(w http.ResponseWriter, r *http.Request, v any) bool {
	r.Body = http.MaxBytesReader(w, r.Body, 1<<20)
	if err := json.NewDecoder(r.Body).Decode(v); err != nil {
		http.Error(w, "bad request", http.StatusBadRequest)
		return false
	}
	return true
}

func reply(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(v)
}

// --- handlers ----------------------------------------------------------------

func (s *server) upsert(w http.ResponseWriter, r *http.Request) {
	var in struct {
		Token        string  `json:"token"`
		Plan         string  `json:"plan"`
		Source       string  `json:"source"`
		Ready        bool    `json:"ready"`
		Status       string  `json:"status"`
		Quota        int     `json:"quota"`
		QuotaUnknown bool    `json:"quota_unknown"`
		LastUsedAt   *string `json:"last_used_at"`
		RestoreAt    *string `json:"restore_at"`
	}
	if !decode(w, r, &in) {
		return
	}
	plan := strings.ToLower(in.Plan)
	src := strings.ToLower(in.Source)
	b := scheduler.Bucket{Plan: plan, Source: src}
	changed := s.ix.Upsert(in.Token, b, in.Ready)

	if s.st != nil {
		status := in.Status
		if status == "" {
			if in.Ready {
				status = "normal"
			} else {
				status = "disabled"
			}
		}
		acc := store.Account{
			AccessToken:  in.Token,
			Plan:         plan,
			Source:       src,
			Status:       status,
			Quota:        in.Quota,
			QuotaUnknown: in.QuotaUnknown,
		}
		if in.LastUsedAt != nil {
			if t, err := time.Parse(time.RFC3339, *in.LastUsedAt); err == nil {
				acc.LastUsedAt = &t
			}
		}
		if in.RestoreAt != nil {
			if t, err := time.Parse(time.RFC3339, *in.RestoreAt); err == nil {
				acc.RestoreAt = &t
			}
		}
		if err := s.st.Upsert(r.Context(), acc); err != nil {
			log.Printf("store upsert %s: %v", in.Token[:min(8, len(in.Token))], err)
		}
	}

	reply(w, map[string]bool{"changed": changed})
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}

func (s *server) remove(w http.ResponseWriter, r *http.Request) {
	var in struct {
		Token string `json:"token"`
	}
	if !decode(w, r, &in) {
		return
	}
	removed := s.ix.Remove(in.Token)
	if s.st != nil && removed {
		if err := s.st.Delete(r.Context(), in.Token); err != nil {
			log.Printf("store delete %s: %v", in.Token[:min(8, len(in.Token))], err)
		}
	}
	reply(w, map[string]bool{"removed": removed})
}

func (s *server) acquire(w http.ResponseWriter, r *http.Request) {
	var in struct {
		Plans    []string `json:"plans"`
		Sources  []string `json:"sources"`
		Excluded []string `json:"excluded"`
		Limit    int      `json:"limit"`
	}
	if !decode(w, r, &in) {
		return
	}
	excluded := map[string]struct{}{}
	for _, t := range in.Excluded {
		excluded[t] = struct{}{}
	}
	tok, ok := s.ix.Acquire(set(in.Plans), set(in.Sources), excluded, in.Limit)
	reply(w, map[string]any{"ok": ok, "token": tok})
}

func (s *server) release(w http.ResponseWriter, r *http.Request) {
	var in struct {
		Token string `json:"token"`
		Limit int    `json:"limit"`
	}
	if !decode(w, r, &in) {
		return
	}
	s.ix.Release(in.Token, in.Limit)
	reply(w, map[string]bool{"ok": true})
}

func (s *server) stats(w http.ResponseWriter, _ *http.Request) {
	out := map[string]any{"index": s.ix.Stats()}
	if s.st != nil {
		if counts, err := s.st.CountByStatus(context.Background()); err == nil {
			out["db"] = counts
		}
	}
	reply(w, out)
}

// --- startup -----------------------------------------------------------------

func loadFromDB(ctx context.Context, st *store.Store, ix *scheduler.Index) error {
	accounts, err := st.LoadAll(ctx)
	if err != nil {
		return err
	}
	ready := 0
	for _, a := range accounts {
		isReady := (a.Status == "normal") && (a.QuotaUnknown || a.Quota > 0)
		if a.RestoreAt != nil && a.Status == "limited" {
			if a.RestoreAt.After(time.Now()) {
				isReady = false
			} else {
				isReady = true
			}
		}
		b := scheduler.Bucket{Plan: a.Plan, Source: a.Source}
		ix.Upsert(a.AccessToken, b, isReady)
		if isReady {
			ready++
		}
	}
	log.Printf("gateway: loaded %d accounts (%d ready) from PostgreSQL", len(accounts), ready)
	return nil
}

func main() {
	token := os.Getenv("GATEWAY_TOKEN")
	if len(token) < 16 {
		log.Fatal("GATEWAY_TOKEN must be set to at least 16 characters")
	}
	addr := os.Getenv("GATEWAY_ADDR")
	if addr == "" {
		addr = "127.0.0.1:8081"
	}

	ix := scheduler.NewIndex(32)
	var st *store.Store

	if dsn := os.Getenv("DATABASE_URL"); dsn != "" {
		ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
		defer cancel()
		var err error
		st, err = store.New(ctx, dsn)
		if err != nil {
			log.Fatalf("gateway: connect to PostgreSQL: %v", err)
		}
		defer st.Close()
		if err = loadFromDB(ctx, st, ix); err != nil {
			log.Fatalf("gateway: load accounts: %v", err)
		}
	} else {
		log.Println("gateway: DATABASE_URL not set, running with in-memory index only")
	}

	s := &server{ix: ix, st: st, token: token}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		reply(w, map[string]bool{"ok": true})
	})
	mux.HandleFunc("GET /v1/stats", s.auth(s.stats))
	mux.HandleFunc("POST /v1/accounts/upsert", s.auth(s.upsert))
	mux.HandleFunc("POST /v1/accounts/remove", s.auth(s.remove))
	mux.HandleFunc("POST /v1/lease/acquire", s.auth(s.acquire))
	mux.HandleFunc("POST /v1/lease/release", s.auth(s.release))

	srv := &http.Server{
		Addr:              addr,
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       15 * time.Second,
		WriteTimeout:      15 * time.Second,
		IdleTimeout:       60 * time.Second,
	}
	log.Printf("gateway listening on %s", addr)
	log.Fatal(srv.ListenAndServe())
}
