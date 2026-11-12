// Package scheduler is the Go port of the account selection index.
//
// It answers "who is ready" with bucketed FIFO queues, smooth weighted
// round-robin across buckets and per-account lease limits. Scoring and the
// least-recently-used ordering live in separate files.
package scheduler

import (
	"sort"
	"sync"
)

// Bucket groups accounts by plan and source.
type Bucket struct {
	Plan   string
	Source string
}

type queueItem struct {
	token string
	epoch uint64
}

type entry struct {
	epoch  uint64
	bucket Bucket
	ready  bool
}

type bucketState struct {
	queue  []queueItem
	head   int
	size   int
	weight int
}

// Stats is a read-only snapshot for diagnostics.
type Stats struct {
	Tracked   int `json:"tracked"`
	Ready     int `json:"ready"`
	Buckets   int `json:"buckets"`
	Inflight  int `json:"inflight"`
	Saturated int `json:"saturated"`
	Acquires  int `json:"acquires"`
	Scanned   int `json:"scanned"`
}

// Index is a fair, lease-based selection index. It is safe for concurrent use.
type Index struct {
	mu         sync.Mutex
	meta       map[string]*entry
	buckets    map[Bucket]*bucketState
	inflight   map[string]int
	saturated  map[string]struct{}
	epoch      uint64
	scanBudget int
	acquires   int
	scanned    int
}

// NewIndex creates an index; scanBudget bounds queue entries examined per take.
func NewIndex(scanBudget int) *Index {
	if scanBudget < 1 {
		scanBudget = 32
	}
	return &Index{
		meta:       map[string]*entry{},
		buckets:    map[Bucket]*bucketState{},
		inflight:   map[string]int{},
		saturated:  map[string]struct{}{},
		scanBudget: scanBudget,
	}
}

func (ix *Index) state(b Bucket) *bucketState {
	s, ok := ix.buckets[b]
	if !ok {
		s = &bucketState{}
		ix.buckets[b] = s
	}
	return s
}

// Upsert inserts or re-classifies one account and reports whether state changed.
// ready=false keeps the account tracked but out of every queue.
func (ix *Index) Upsert(token string, bucket Bucket, ready bool) bool {
	if token == "" {
		return false
	}
	ix.mu.Lock()
	defer ix.mu.Unlock()
	prev := ix.meta[token]
	if prev != nil {
		if prev.bucket == bucket && prev.ready == ready {
			return false
		}
		if prev.ready {
			ix.state(prev.bucket).size--
		}
	}
	ix.epoch++
	ix.meta[token] = &entry{epoch: ix.epoch, bucket: bucket, ready: ready}
	if ready {
		s := ix.state(bucket)
		s.size++
		if _, held := ix.saturated[token]; !held {
			s.queue = append(s.queue, queueItem{token, ix.epoch})
		}
	}
	return true
}

// Remove drops an account and its lease bookkeeping.
func (ix *Index) Remove(token string) bool {
	ix.mu.Lock()
	defer ix.mu.Unlock()
	prev := ix.meta[token]
	if prev == nil {
		return false
	}
	if prev.ready {
		ix.state(prev.bucket).size--
	}
	delete(ix.meta, token)
	delete(ix.inflight, token)
	delete(ix.saturated, token)
	return true
}

// HasReady reports whether any ready account exists in the allowed buckets.
func (ix *Index) HasReady(plans, sources map[string]struct{}) bool {
	ix.mu.Lock()
	defer ix.mu.Unlock()
	return len(ix.eligible(plans, sources)) > 0
}

func allowed(set map[string]struct{}, v string) bool {
	if len(set) == 0 {
		return true
	}
	_, ok := set[v]
	return ok
}

func (ix *Index) eligible(plans, sources map[string]struct{}) []Bucket {
	out := make([]Bucket, 0, len(ix.buckets))
	for b, s := range ix.buckets {
		if s.size > 0 && allowed(plans, b.Plan) && allowed(sources, b.Source) {
			out = append(out, b)
		}
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Plan != out[j].Plan {
			return out[i].Plan < out[j].Plan
		}
		return out[i].Source < out[j].Source
	})
	return out
}

// pick runs one step of smooth weighted round-robin weighted by ready count.
func (ix *Index) pick(cands []Bucket) int {
	total, best := 0, 0
	for i, b := range cands {
		s := ix.buckets[b]
		s.weight += s.size
		total += s.size
		if s.weight > ix.buckets[cands[best]].weight {
			best = i
		}
	}
	ix.buckets[cands[best]].weight -= total
	return best
}

// Acquire leases the next ready account that is not excluded and still has
// spare capacity (limit concurrent leases per account).
func (ix *Index) Acquire(plans, sources map[string]struct{}, excluded map[string]struct{}, limit int) (string, bool) {
	if limit < 1 {
		limit = 1
	}
	ix.mu.Lock()
	defer ix.mu.Unlock()
	cands := ix.eligible(plans, sources)
	for len(cands) > 0 {
		i := ix.pick(cands)
		if token, ok := ix.take(cands[i], excluded, limit); ok {
			ix.acquires++
			return token, true
		}
		cands = append(cands[:i], cands[i+1:]...)
	}
	return "", false
}

func (ix *Index) take(b Bucket, excluded map[string]struct{}, limit int) (string, bool) {
	s := ix.buckets[b]
	for n := 0; n < ix.scanBudget && s.head < len(s.queue); n++ {
		item := s.queue[s.head]
		s.head++
		ix.scanned++
		m := ix.meta[item.token]
		if m == nil || m.epoch != item.epoch || !m.ready || m.bucket != b {
			continue // stale entry: removed, re-bucketed or re-queued
		}
		if _, skip := excluded[item.token]; skip {
			s.queue = append(s.queue, item)
			continue
		}
		ix.inflight[item.token]++
		if ix.inflight[item.token] >= limit {
			ix.saturated[item.token] = struct{}{}
		} else {
			s.queue = append(s.queue, item)
		}
		ix.compact(s)
		return item.token, true
	}
	ix.compact(s)
	return "", false
}

// compact reclaims the consumed prefix once it dominates the slice.
func (ix *Index) compact(s *bucketState) {
	if s.head > 1024 && s.head*2 > len(s.queue) {
		s.queue = append([]queueItem(nil), s.queue[s.head:]...)
		s.head = 0
	}
}

// Release returns one lease; a saturated account re-enters its queue once
// it has spare capacity again.
func (ix *Index) Release(token string, limit int) {
	if limit < 1 {
		limit = 1
	}
	ix.mu.Lock()
	defer ix.mu.Unlock()
	remaining := 0
	if n := ix.inflight[token]; n > 1 {
		remaining = n - 1
		ix.inflight[token] = remaining
	} else {
		delete(ix.inflight, token)
	}
	if _, held := ix.saturated[token]; !held || remaining >= limit {
		return
	}
	delete(ix.saturated, token)
	if m := ix.meta[token]; m != nil && m.ready {
		s := ix.state(m.bucket)
		s.queue = append(s.queue, queueItem{token, m.epoch})
	}
}

// Stats returns a diagnostics snapshot.
func (ix *Index) Stats() Stats {
	ix.mu.Lock()
	defer ix.mu.Unlock()
	st := Stats{
		Tracked:   len(ix.meta),
		Inflight:  len(ix.inflight),
		Saturated: len(ix.saturated),
		Acquires:  ix.acquires,
		Scanned:   ix.scanned,
	}
	for _, s := range ix.buckets {
		if s.size > 0 {
			st.Ready += s.size
			st.Buckets++
		}
	}
	return st
}
