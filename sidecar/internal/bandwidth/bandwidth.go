// Package bandwidth holds rate estimators and a min-composite, mirroring the
// shape of shared/bandwidth on the Python side. The estimators live in the
// edge sidecar (alongside the policy that consults them); the server sidecar
// only emits raw observations as FBCK control messages.
package bandwidth

// Estimator returns a target send rate in bits per second.
//
// Implementations are concurrency-safe: TargetBps() is read on the hot send
// path and writes happen from sample/feedback goroutines.
type Estimator interface {
	TargetBps() uint64
}
