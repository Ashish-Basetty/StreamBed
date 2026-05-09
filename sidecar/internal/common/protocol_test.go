package common

import "testing"

func TestClassifyPrefix(t *testing.T) {
	cases := []struct {
		name string
		in   []byte
		want PacketKind
	}{
		{"chnk", []byte("CHNKxxxx"), KindLossyData},
		{"embd", []byte("EMBDxxxx"), KindLosslessData},
		{"rate", []byte("RATE{...}"), KindControl},
		{"actn", []byte("ACTN{...}"), KindControl},
		{"fbck", []byte("FBCK\x00\x00\x00\x00\x00\x00\x00\x00"), KindControl},
		{"cstl", []byte("CSTLxxxx"), KindLossyData},
		{"cstr", []byte("CSTRxxxx"), KindLosslessData},
		{"unknown-json", []byte(`{"hello":1}`), KindUnknown},
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
