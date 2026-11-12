package scheduler

import (
	"math"
	"time"
)

// Account carries the runtime signals used to rank ready candidates.
// Zero values fall back to the same neutral defaults as the Python scorer.
type Account struct {
	SuccessEWMA   float64   // 0..1, default 0.9
	LatencyEWMAms float64   // default 45000
	Egress        float64   // 0..1 egress success, default 0.9
	Capability    float64   // 0..1 soft capability match, default 1
	PlanFactor    float64   // small paid-plan bonus, default 1
	LastUsed      time.Time // zero means never used
}

func clamp01(v float64) float64 {
	return math.Max(0, math.Min(1, v))
}

func orDefault(v, d float64) float64 {
	if v == 0 {
		return d
	}
	return v
}

// LatencyScore is a 0..1 speed score; 45s is the neutral midpoint.
func LatencyScore(ms float64) float64 {
	ms = math.Max(0, orDefault(ms, 45000))
	return clamp01(1.0 / (1.0 + ms/45000.0))
}

// FreshnessScore is 1.0 for never-used or idle >= 24h accounts and 0.0 for
// an account that was just used. It keeps the pool evenly consumed instead
// of re-picking the same proven accounts.
func FreshnessScore(last time.Time, now time.Time) float64 {
	if last.IsZero() {
		return 1.0
	}
	idle := now.Sub(last).Seconds()
	return clamp01(idle / 86400.0)
}

// Score ranks one candidate; higher is better. It is deterministic, so the
// caller adds its own exploration (for example a small random pick).
func Score(a Account, now time.Time) float64 {
	health := clamp01(orDefault(a.SuccessEWMA, 0.9))
	capability := clamp01(orDefault(a.Capability, 1))
	egress := clamp01(orDefault(a.Egress, 0.9))
	plan := orDefault(a.PlanFactor, 1)
	fresh := FreshnessScore(a.LastUsed, now)
	score := health *
		(0.15 + 0.85*capability) *
		(0.5 + 0.5*egress) *
		(0.8 + 0.2*LatencyScore(a.LatencyEWMAms)) *
		(1.0 + 0.35*fresh) *
		plan
	return math.Max(0, score)
}

// Best returns the index of the highest-scored candidate, or -1 if empty.
func Best(cands []Account, now time.Time) int {
	best, bestScore := -1, -1.0
	for i, c := range cands {
		if s := Score(c, now); s > bestScore {
			best, bestScore = i, s
		}
	}
	return best
}
