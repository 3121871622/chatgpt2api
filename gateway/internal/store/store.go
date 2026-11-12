// Package store handles durable account persistence in PostgreSQL.
//
// Schema (auto-created on first run):
//
//	CREATE TABLE accounts (
//	    access_token TEXT PRIMARY KEY,
//	    plan         TEXT NOT NULL DEFAULT '',
//	    source       TEXT NOT NULL DEFAULT '',
//	    status       TEXT NOT NULL DEFAULT 'normal',
//	    quota        INT  NOT NULL DEFAULT 0,
//	    quota_unknown BOOL NOT NULL DEFAULT FALSE,
//	    last_used_at  TIMESTAMPTZ,
//	    restore_at    TIMESTAMPTZ,
//	    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
//	);
package store

import (
	"context"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

const schema = `
CREATE TABLE IF NOT EXISTS gw_accounts (
    access_token  TEXT        PRIMARY KEY,
    plan          TEXT        NOT NULL DEFAULT '',
    source        TEXT        NOT NULL DEFAULT '',
    status        TEXT        NOT NULL DEFAULT 'normal',
    quota         INT         NOT NULL DEFAULT 0,
    quota_unknown BOOL        NOT NULL DEFAULT FALSE,
    last_used_at  TIMESTAMPTZ,
    restore_at    TIMESTAMPTZ,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_gw_accounts_status ON gw_accounts (status);
CREATE INDEX IF NOT EXISTS idx_gw_accounts_restore_at ON gw_accounts (restore_at) WHERE restore_at IS NOT NULL;
`

// Account mirrors one row in the accounts table.
type Account struct {
	AccessToken  string
	Plan         string
	Source       string
	Status       string // "normal" | "limited" | "disabled" | "invalid"
	Quota        int
	QuotaUnknown bool
	LastUsedAt   *time.Time
	RestoreAt    *time.Time
	UpdatedAt    time.Time
}

// Store wraps a pgxpool and owns the accounts table.
type Store struct {
	pool *pgxpool.Pool
}

// New opens a pgxpool and migrates the schema.
func New(ctx context.Context, dsn string) (*Store, error) {
	pool, err := pgxpool.New(ctx, dsn)
	if err != nil {
		return nil, fmt.Errorf("store: open pool: %w", err)
	}
	if err = pool.Ping(ctx); err != nil {
		pool.Close()
		return nil, fmt.Errorf("store: ping: %w", err)
	}
	if _, err = pool.Exec(ctx, schema); err != nil {
		pool.Close()
		return nil, fmt.Errorf("store: migrate: %w", err)
	}
	return &Store{pool: pool}, nil
}

// Close releases all pool connections.
func (s *Store) Close() { s.pool.Close() }

// Upsert inserts or updates one account row.
func (s *Store) Upsert(ctx context.Context, a Account) error {
	_, err := s.pool.Exec(ctx, `
		INSERT INTO gw_accounts
			(access_token, plan, source, status, quota, quota_unknown, last_used_at, restore_at, updated_at)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8,now())
		ON CONFLICT (access_token) DO UPDATE SET
			plan          = EXCLUDED.plan,
			source        = EXCLUDED.source,
			status        = EXCLUDED.status,
			quota         = EXCLUDED.quota,
			quota_unknown = EXCLUDED.quota_unknown,
			last_used_at  = EXCLUDED.last_used_at,
			restore_at    = EXCLUDED.restore_at,
			updated_at    = now()`,
		a.AccessToken, a.Plan, a.Source, a.Status,
		a.Quota, a.QuotaUnknown, a.LastUsedAt, a.RestoreAt,
	)
	return err
}

// Delete removes one account by access token.
func (s *Store) Delete(ctx context.Context, accessToken string) error {
	_, err := s.pool.Exec(ctx, `DELETE FROM gw_accounts WHERE access_token = $1`, accessToken)
	return err
}

// LoadAll returns every account row, sorted by last_used_at ascending (never-used first).
func (s *Store) LoadAll(ctx context.Context) ([]Account, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT access_token, plan, source, status, quota, quota_unknown, last_used_at, restore_at, updated_at
		FROM gw_accounts
		ORDER BY last_used_at ASC NULLS FIRST`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	return pgx.CollectRows(rows, func(row pgx.CollectableRow) (Account, error) {
		var a Account
		return a, row.Scan(
			&a.AccessToken, &a.Plan, &a.Source, &a.Status,
			&a.Quota, &a.QuotaUnknown, &a.LastUsedAt, &a.RestoreAt, &a.UpdatedAt,
		)
	})
}

// CountByStatus returns a map of status → count for diagnostics.
func (s *Store) CountByStatus(ctx context.Context) (map[string]int, error) {
	rows, err := s.pool.Query(ctx, `SELECT status, count(*) FROM gw_accounts GROUP BY status`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[string]int{}
	for rows.Next() {
		var status string
		var n int
		if err = rows.Scan(&status, &n); err != nil {
			return nil, err
		}
		out[status] = n
	}
	return out, rows.Err()
}
