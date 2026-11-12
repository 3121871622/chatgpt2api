package scheduler

import (
	"fmt"
	"testing"
	"time"
)

var free = Bucket{Plan: "free", Source: "web"}

func seed(ix *Index, n int) {
	for i := 0; i < n; i++ {
		ix.Upsert(fmt.Sprintf("tok-%d", i), free, true)
	}
}

func TestLeaseAndRelease(t *testing.T) {
	ix := NewIndex(32)
	seed(ix, 10)
	seen := map[string]bool{}
	for i := 0; i < 10; i++ {
		tok, ok := ix.Acquire(nil, nil, nil, 1)
		if !ok {
			t.Fatalf("acquire %d failed", i)
		}
		seen[tok] = true
	}
	if len(seen) != 10 {
		t.Fatalf("expected 10 distinct leases, got %d", len(seen))
	}
	if _, ok := ix.Acquire(nil, nil, nil, 1); ok {
		t.Fatal("11th acquire must fail while every account is leased")
	}
	ix.Release("tok-3", 1)
	tok, ok := ix.Acquire(nil, nil, nil, 1)
	if !ok || tok != "tok-3" {
		t.Fatalf("released account should be leasable again, got %q ok=%v", tok, ok)
	}
}

func TestRoundRobinCoversWholePool(t *testing.T) {
	ix := NewIndex(32)
	seed(ix, 50)
	seen := map[string]bool{}
	for i := 0; i < 50; i++ {
		tok, ok := ix.Acquire(nil, nil, nil, 2)
		if !ok {
			t.Fatal("acquire failed")
		}
		seen[tok] = true
		ix.Release(tok, 2)
	}
	if len(seen) != 50 {
		t.Fatalf("every account should be reached once per cycle, got %d/50", len(seen))
	}
}

func TestExcludedIsSkipped(t *testing.T) {
	ix := NewIndex(32)
	seed(ix, 2)
	tok, ok := ix.Acquire(nil, nil, map[string]struct{}{"tok-0": {}}, 1)
	if !ok || tok != "tok-1" {
		t.Fatalf("expected tok-1, got %q ok=%v", tok, ok)
	}
}

func TestNotReadyAccountsAreNeverServed(t *testing.T) {
	ix := NewIndex(32)
	ix.Upsert("a", free, false)
	if _, ok := ix.Acquire(nil, nil, nil, 1); ok {
		t.Fatal("a not-ready account must not be leased")
	}
	if ix.HasReady(nil, nil) {
		t.Fatal("HasReady must be false")
	}
	ix.Upsert("a", free, true)
	if tok, ok := ix.Acquire(nil, nil, nil, 1); !ok || tok != "a" {
		t.Fatalf("account should be served once ready, got %q ok=%v", tok, ok)
	}
}

func TestPlanFilterAndRemove(t *testing.T) {
	ix := NewIndex(32)
	ix.Upsert("f", free, true)
	ix.Upsert("p", Bucket{Plan: "plus", Source: "web"}, true)
	plus := map[string]struct{}{"plus": {}}
	if tok, ok := ix.Acquire(plus, nil, nil, 1); !ok || tok != "p" {
		t.Fatalf("plus filter should return p, got %q ok=%v", tok, ok)
	}
	if !ix.Remove("f") {
		t.Fatal("remove should report a change")
	}
	if _, ok := ix.Acquire(nil, nil, nil, 1); ok {
		t.Fatal("nothing should remain: p is leased and f was removed")
	}
	if st := ix.Stats(); st.Tracked != 1 {
		t.Fatalf("expected 1 tracked account, got %d", st.Tracked)
	}
}

func TestReclassifyMovesAccountBetweenBuckets(t *testing.T) {
	ix := NewIndex(32)
	ix.Upsert("a", free, true)
	ix.Upsert("a", Bucket{Plan: "plus", Source: "web"}, true)
	if _, ok := ix.Acquire(map[string]struct{}{"free": {}}, nil, nil, 1); ok {
		t.Fatal("account moved to plus must not be served from the free bucket")
	}
	if tok, ok := ix.Acquire(map[string]struct{}{"plus": {}}, nil, nil, 1); !ok || tok != "a" {
		t.Fatalf("expected a from plus, got %q ok=%v", tok, ok)
	}
}

func TestFreshAccountOutranksRecentlyUsedOne(t *testing.T) {
	now := time.Now()
	established := Account{SuccessEWMA: 1.0, LastUsed: now.Add(-30 * time.Second)}
	fresh := Account{SuccessEWMA: 0.92}
	if Best([]Account{established, fresh}, now) != 1 {
		t.Fatal("a never-used account should beat a just-used, slightly healthier one")
	}
}

func TestFreshnessBounds(t *testing.T) {
	now := time.Now()
	if FreshnessScore(time.Time{}, now) != 1 {
		t.Fatal("never used must score 1")
	}
	if FreshnessScore(now.Add(-48*time.Hour), now) != 1 {
		t.Fatal("idle over 24h must clamp to 1")
	}
	if FreshnessScore(now, now) != 0 {
		t.Fatal("just used must score 0")
	}
	if Best(nil, now) != -1 {
		t.Fatal("empty candidate list returns -1")
	}
}
