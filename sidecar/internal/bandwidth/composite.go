package bandwidth

// Composite returns the minimum TargetBps across its members. Mirror of the
// Python CompositeBackend: the most conservative estimate wins so we never
// exceed any single backend's read on the link.
type Composite struct {
	members []Estimator
}

func NewComposite(members ...Estimator) *Composite {
	return &Composite{members: members}
}

func (c *Composite) TargetBps() uint64 {
	if len(c.members) == 0 {
		return 10_000
	}
	out := c.members[0].TargetBps()
	for _, m := range c.members[1:] {
		if v := m.TargetBps(); v < out {
			out = v
		}
	}
	return out
}
