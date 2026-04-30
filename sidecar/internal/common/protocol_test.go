package common

import "testing"

func TestClassifyPrefix(t *testing.T) {
	cases := []struct {
		name string
		in   []byte
		want PacketKind
	}{
		{"chnk", []byte("CHNKxxxx"), KindData},
		{"rate", []byte("RATE{...}"), KindControl},
		{"actn", []byte("ACTN{...}"), KindControl},
		{"json-feedback", []byte(`{"received_bps":1}`), KindControl},
		{"empty", []byte(""), KindUnknown},
		{"short", []byte("CHN"), KindUnknown},
		{"random", []byte("\x00\x01\x02\x03..."), KindUnknown},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := ClassifyPrefix(c.in); got != c.want {
				t.Fatalf("ClassifyPrefix(%q) = %d, want %d", c.in, got, c.want)
			}
		})
	}
}
