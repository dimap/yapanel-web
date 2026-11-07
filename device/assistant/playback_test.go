package main

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"sync"
	"testing"
	"time"
)

// Execute this test binary as a predictable player: either consume PCM or
// deliberately stop reading and ignore SIGINT to exercise forced termination.
func TestPCMPlaybackHelper(t *testing.T) {
	switch os.Getenv("PCM_TEST_HELPER") {
	case "drain":
		f, err := os.Create(os.Getenv("PCM_TEST_OUT"))
		if err != nil {
			os.Exit(2)
		}
		_, err = io.Copy(f, os.Stdin)
		f.Close()
		if err != nil {
			os.Exit(3)
		}
		os.Exit(0)
	case "block":
		signal.Ignore(os.Interrupt)
		fmt.Fprintln(os.Stdout, "ready")
		time.Sleep(time.Hour)
		os.Exit(0)
	}
}

type playerReady struct {
	once sync.Once
	ch   chan struct{}
}

func (p *playerReady) Write(data []byte) (int, error) {
	p.once.Do(func() { close(p.ch) })
	return len(data), nil
}
func helperCommand(ctx context.Context, mode, out string) *exec.Cmd {
	cmd := exec.CommandContext(ctx, os.Args[0], "-test.run=^TestPCMPlaybackHelper$")
	cmd.Env = append(os.Environ(), "PCM_TEST_HELPER="+mode, "PCM_TEST_OUT="+out)
	return cmd
}
func TestPCMPlaybackDrainsSuccessfulResponse(t *testing.T) {
	file := filepath.Join(t.TempDir(), "received.pcm")
	p := pcmPlayback{ctx: context.Background(), command: func(ctx context.Context) *exec.Cmd { return helperCommand(ctx, "drain", file) }}
	want := []byte{1, 0, 2, 0, 3, 0, 4, 0}
	if err := p.Write(want[:4]); err != nil {
		t.Fatal(err)
	}
	if err := p.Write(want[4:]); err != nil {
		t.Fatal(err)
	}
	if err := p.Close(); err != nil {
		t.Fatal(err)
	}
	got, err := os.ReadFile(file)
	if err != nil || !bytes.Equal(got, want) {
		t.Fatalf("PCM changed or truncated: %v %v", got, err)
	}
}
func TestPCMPlaybackAbortUnblocksSlowConsumer(t *testing.T) {
	ready := &playerReady{ch: make(chan struct{})}
	p := pcmPlayback{ctx: context.Background(), command: func(ctx context.Context) *exec.Cmd {
		cmd := helperCommand(ctx, "block", "")
		cmd.Stdout = ready
		return cmd
	}}
	written := make(chan error, 1)
	go func() { written <- p.Write(make([]byte, 4<<20)) }()
	select {
	case <-ready.ch:
	case <-time.After(5 * time.Second):
		t.Fatal("player did not start")
	}
	started := time.Now()
	p.Abort()
	select {
	case err := <-written:
		if err == nil {
			t.Error("blocked write succeeded after abort")
		}
	case <-time.After(5 * time.Second):
		t.Fatal("abort did not release writer")
	}
	if err := p.Close(); err == nil {
		t.Error("aborted player reported success")
	}
	if time.Since(started) > 5*time.Second {
		t.Fatal("termination exceeded bound")
	}
}
