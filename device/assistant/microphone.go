package main

import (
	"context"
	"encoding/binary"
	"errors"
	"fmt"
	"math"
	"net"
	"os"
	"path/filepath"
	"time"
)

const microphoneDir = "/tmp/webpanel-mic"
const microphoneSocket = microphoneDir + "/audio.sock"
const microphoneLease = microphoneDir + "/exclusive"

// decodeMicrophone rejects unknown formats instead of treating hardware/feedback
// channels as speech. Stock multi_capture delivers mic0, mic1, speaker feedback.
func decodeMicrophone(packet []byte, gain float64) ([]byte, uint32, error) {
	if len(packet) < 20 || string(packet[:4]) != "WPM1" || binary.LittleEndian.Uint32(packet[4:]) != 48000 || binary.LittleEndian.Uint16(packet[8:]) != 3 || binary.LittleEndian.Uint16(packet[10:]) != 16 {
		return nil, 0, errors.New("неподдерживаемый формат микрофонов")
	}
	frames := int(binary.LittleEndian.Uint32(packet[12:]))
	seq := binary.LittleEndian.Uint32(packet[16:])
	if frames < 2 || frames > 1024 || frames%2 != 0 || len(packet) != 20+frames*6 {
		return nil, seq, errors.New("повреждённый пакет микрофонов")
	}
	out := make([]byte, frames) // 48 kHz -> 24 kHz, mono PCM16, two frames/sample
	for i := 0; i < frames; i += 2 {
		sum := 0
		for j := 0; j < 2; j++ {
			for ch := 0; ch < 2; ch++ {
				sum += int(int16(binary.LittleEndian.Uint16(packet[20+(i+j)*6+ch*2:])))
			}
		}
		v := int(math.Round(float64(sum) * gain / 4))
		if v > 32767 {
			v = 32767
		}
		if v < -32768 {
			v = -32768
		}
		binary.LittleEndian.PutUint16(out[i:], uint16(int16(v)))
	}
	return out, seq, nil
}

// Local endpoint detection bounds each request and keeps a short pre-roll.
// Frames from our own playback are never fed to STT or a voice model.
type utteranceDetector struct {
	audio, pre    []byte
	voiced, quiet int
	noise         float64
	warm          int
}

func pcmRMS(pcm []byte) float64 {
	if len(pcm) < 2 {
		return 0
	}
	sum := 0.0
	for i := 0; i < len(pcm); i += 2 {
		s := float64(int16(binary.LittleEndian.Uint16(pcm[i:])))
		sum += s * s
	}
	return math.Sqrt(sum / float64(len(pcm)/2))
}

func (v *utteranceDetector) push(pcm []byte, c Microphone) []byte {
	samples := len(pcm) / 2
	if samples == 0 {
		return nil
	}
	rms := pcmRMS(pcm)
	if v.warm < 12000 {
		v.warm += samples
		v.noise += (rms - v.noise) * 0.15
	}
	threshold := math.Max(c.Threshold, v.noise*3)
	speech := rms > threshold && v.warm >= 12000
	if len(v.audio) == 0 {
		v.pre = append(v.pre, pcm...)
		if len(v.pre) > 12000 {
			v.pre = v.pre[len(v.pre)-12000:]
		}
		if !speech {
			v.noise += (rms - v.noise) * 0.03
			return nil
		}
		v.audio = append(v.audio, v.pre...)
		v.pre = nil
	} else {
		v.audio = append(v.audio, pcm...)
	}
	if speech {
		v.voiced += samples
		v.quiet = 0
	} else {
		v.quiet += samples
	}
	if v.quiet < 24*c.SilenceMS && len(v.audio) < 24*2*20000 {
		return nil
	}
	audio := v.audio
	enough := v.voiced >= 24*180
	v.audio, v.pre, v.voiced, v.quiet = nil, nil, 0, 0
	if !enough {
		return nil
	}
	return audio
}

func openMicrophoneSocket(path string) (*net.UnixConn, error) {
	dir := filepath.Dir(path)
	if st, err := os.Lstat(dir); err == nil && (!st.IsDir() || st.Mode()&os.ModeSymlink != 0) {
		return nil, errors.New("небезопасная папка микрофона")
	}
	if err := os.MkdirAll(dir, 0700); err != nil {
		return nil, err
	}
	if err := os.Chmod(dir, 0700); err != nil {
		return nil, err
	}
	if _, err := os.Lstat(path); err == nil {
		conn, e := net.DialUnix("unixgram", nil, &net.UnixAddr{Name: path, Net: "unixgram"})
		if e == nil {
			_, e = conn.Write([]byte{})
			conn.Close()
		}
		if e == nil {
			return nil, errors.New("микрофон уже обслуживает другой процесс")
		}
		// Only a stale socket may be removed, never an arbitrary file.
		if st, _ := os.Lstat(path); st == nil || st.Mode()&os.ModeSocket == 0 {
			return nil, errors.New("путь микрофона занят")
		}
		if err := os.Remove(path); err != nil {
			return nil, err
		}
	}
	c, err := net.ListenUnixgram("unixgram", &net.UnixAddr{Name: path, Net: "unixgram"})
	if err != nil {
		return nil, err
	}
	if err = os.Chmod(path, 0600); err != nil {
		c.Close()
		os.Remove(path)
		return nil, err
	}
	c.SetReadBuffer(256 << 10)
	return c, nil
}

func (d *Daemon) microphone(ctx context.Context) {
	conn, err := openMicrophoneSocket(microphoneSocket)
	if err != nil {
		d.microphoneError(err)
		return
	}
	defer conn.Close()
	defer os.Remove(microphoneSocket)
	defer os.Remove(microphoneLease)
	defer os.Remove(microphoneDir + "/ready")
	go func() { <-ctx.Done(); conn.Close() }()
	var detector utteranceDetector
	noiseFloor := 0.0
	lastGain := 0.0
	var last, lease, heartbeat, silenceCalibration time.Time
	var seq uint32
	validSeq := false
	buf := make([]byte, 16<<10)
	for ctx.Err() == nil {
		conn.SetReadDeadline(time.Now().Add(250 * time.Millisecond))
		n, _, e := conn.ReadFromUnix(buf)
		d.mu.Lock()
		c := d.cfg
		own := c.Enabled && c.Microphone.Source == "microphone" && d.mode && (c.Mode.FollowUp || d.micArmed || d.state != "idle")
		listen := own && !d.outputActive && time.Since(d.noSpeechAt) > 1500*time.Millisecond && (d.state == "idle" || d.state == "listening") && time.Since(d.playbackEnd) > 600*time.Millisecond
		if d.noSpeechAt.After(silenceCalibration) {
			noiseFloor = math.Max(noiseFloor, d.noSpeechLevel)
			silenceCalibration = d.noSpeechAt
			detector = utteranceDetector{noise: noiseFloor, warm: 12000}
		}
		d.mu.Unlock()
		if !own || time.Since(last) > time.Second {
			os.Remove(microphoneLease)
		}
		if e != nil {
			if ctx.Err() != nil {
				return
			}
			if t, ok := e.(net.Error); ok && t.Timeout() {
				if time.Since(last) > 2*time.Second {
					d.microphoneError(errors.New("нет потока микрофонов: подключите захват на колонке"))
				}
				continue
			}
			d.microphoneError(e)
			return
		}
		if n == 0 {
			continue
		}
		pcm, next, e := decodeMicrophone(buf[:n], c.Microphone.Gain)
		if e != nil {
			d.microphoneError(e)
			continue
		}
		if c.Microphone.Gain != lastGain {
			noiseFloor = 0
			lastGain = c.Microphone.Gain
			detector = utteranceDetector{}
		}
		if !own {
			rms := pcmRMS(pcm)
			if noiseFloor == 0 {
				noiseFloor = rms
			} else if rms < noiseFloor {
				noiseFloor += 0.2 * (rms - noiseFloor)
			} else {
				noiseFloor *= 1.002
			}
		}
		last = time.Now()
		if time.Since(heartbeat) > 400*time.Millisecond {
			os.WriteFile(microphoneDir+"/ready", []byte(fmt.Sprint(last.Unix())), 0600)
			heartbeat = last
		}
		d.mu.Lock()
		if !d.micReady || d.micError != "" {
			d.micReady, d.micError, d.dirty = true, "", true
		}
		d.mu.Unlock()
		if own && time.Since(lease) > 400*time.Millisecond {
			if err := os.WriteFile(microphoneLease, []byte("active\n"), 0600); err != nil {
				d.microphoneError(err)
				continue
			}
			lease = last
		}
		if !listen {
			detector = utteranceDetector{noise: noiseFloor, warm: 12000}
			seq = next
			validSeq = true
			continue
		}
		if validSeq && next != seq+1 {
			detector = utteranceDetector{}
		}
		seq, validSeq = next, true
		if audio := detector.push(pcm, c.Microphone); len(audio) > 0 {
			d.askAudio(audio)
		}
	}
}
func (d *Daemon) microphoneError(err error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.micError != err.Error() || d.micReady {
		d.micReady, d.micError, d.dirty = false, err.Error(), true
	}
}

func pcmWAV(pcm []byte, rate int) []byte {
	b := make([]byte, 44+len(pcm))
	copy(b, "RIFF")
	binary.LittleEndian.PutUint32(b[4:], uint32(36+len(pcm)))
	copy(b[8:], "WAVEfmt ")
	binary.LittleEndian.PutUint32(b[16:], 16)
	binary.LittleEndian.PutUint16(b[20:], 1)
	binary.LittleEndian.PutUint16(b[22:], 1)
	binary.LittleEndian.PutUint32(b[24:], uint32(rate))
	binary.LittleEndian.PutUint32(b[28:], uint32(rate*2))
	binary.LittleEndian.PutUint16(b[32:], 2)
	binary.LittleEndian.PutUint16(b[34:], 16)
	copy(b[36:], "data")
	binary.LittleEndian.PutUint32(b[40:], uint32(len(pcm)))
	copy(b[44:], pcm)
	return b
}

func (c Config) speechRecognition() (STT, error) {
	s := c.STT
	switch s.Provider {
	case "model":
		s.BaseURL, s.APIKey, s.Proxy = c.LLM.BaseURL, c.LLM.APIKey, c.LLM.Proxy
		if s.Model == "" {
			switch {
			case keyID(s.BaseURL) == "https://api.openai.com/v1":
				s.Model = "gpt-4o-mini-transcribe"
			case keyID(s.BaseURL) == "https://api.groq.com/openai/v1":
				s.Model = "whisper-large-v3-turbo"
			default:
				return s, errors.New("укажите STT-модель выбранного провайдера или свой STT")
			}
		}
	case "custom":
	case "alice", "":
		return s, errors.New("для захвата микрофонов выберите STT провайдера или свой STT")
	default:
		return s, fmt.Errorf("неизвестный STT: %s", s.Provider)
	}
	if s.BaseURL == "" || s.Model == "" {
		return s, errors.New("не заданы адрес API и модель STT")
	}
	return s, nil
}
