package main

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"os"
	"os/exec"
	"time"
)

// aplay consumes native PCM deltas as they arrive, rather than waiting for a WAV.
type pcmPlayback struct {
	ctx     context.Context
	cancel  context.CancelFunc
	command func(context.Context) *exec.Cmd
	cmd     *exec.Cmd
	in      io.WriteCloser
	stderr  bytes.Buffer
}

func (p *pcmPlayback) Write(audio []byte) error {
	if len(audio) == 0 {
		return nil
	}
	if p.cmd == nil {
		if err := p.ctx.Err(); err != nil {
			return err
		}
		ctx, cancel := context.WithCancel(p.ctx)
		p.cancel = cancel
		if p.command != nil {
			p.cmd = p.command(ctx)
		} else {
			p.cmd = exec.CommandContext(ctx, "/usr/bin/aplay", "-q", "-t", "raw", "-f", "S16_LE", "-r", "24000", "-c", "1", "-")
		}
		p.cmd.Cancel = func() error { return p.cmd.Process.Signal(os.Interrupt) }
		p.cmd.WaitDelay = 3 * time.Second
		p.cmd.Stderr = &p.stderr
		var err error
		p.in, err = p.cmd.StdinPipe()
		if err != nil {
			p.cmd = nil
			cancel()
			return err
		}
		if err = p.cmd.Start(); err != nil {
			p.in.Close()
			p.cmd = nil
			cancel()
			return err
		}
	}
	_, err := p.in.Write(audio)
	return err
}

// Abort discards pending sound. Close alone drains a successful response.
func (p *pcmPlayback) Abort() {
	if p.cancel != nil {
		p.cancel()
	}
}
func (p *pcmPlayback) Close() error {
	if p.cancel != nil {
		defer p.cancel()
	}
	if p.cmd == nil {
		return nil
	}
	p.in.Close()
	err := p.cmd.Wait()
	p.cmd = nil
	if err != nil {
		return fmt.Errorf("aplay: %w %.300s", err, p.stderr.String())
	}
	return nil
}
