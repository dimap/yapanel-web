package main

import (
	"crypto/x509"
	"encoding/pem"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestClientFirmwareCA(t *testing.T) {
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()
	dir := t.TempDir()
	bundle := filepath.Join(dir, "firmware.crt")
	if err := os.WriteFile(bundle, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: srv.Certificate().Raw}), 0600); err != nil {
		t.Fatal(err)
	}
	invalid := filepath.Join(dir, "invalid.crt")
	if err := os.WriteFile(invalid, []byte("not a certificate"), 0600); err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		name, ca, url string
		wantOK        bool
	}{
		{"firmware CA trusted", bundle, srv.URL, true},
		{"missing bundle rejects unknown CA", filepath.Join(dir, "missing"), srv.URL, false},
		{"malformed bundle rejects unknown CA", invalid, srv.URL, false},
		{"hostname still verified", bundle, strings.Replace(srv.URL, "127.0.0.1", "localhost", 1), false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			hc, err := clientWithCA("", 5*time.Second, tc.ca)
			if err != nil {
				t.Fatal(err)
			}
			defer hc.CloseIdleConnections()
			resp, err := hc.Get(tc.url)
			if resp != nil {
				resp.Body.Close()
			}
			if (err == nil) != tc.wantOK {
				t.Fatalf("GET %s: %v, want success=%v", tc.url, err, tc.wantOK)
			}
			if !tc.wantOK {
				var unknown x509.UnknownAuthorityError
				var hostname x509.HostnameError
				if tc.ca == bundle {
					if !errors.As(err, &hostname) {
						t.Fatalf("expected hostname error, got %v", err)
					}
				} else if !errors.As(err, &unknown) {
					t.Fatalf("expected unknown authority error, got %v", err)
				}
			}
		})
	}
}
