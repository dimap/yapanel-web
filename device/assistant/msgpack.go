package main

import (
	"encoding/binary"
	"errors"
	"math"
)

// Минимальный msgpack: Fish Audio Live обменивается им в бинарных кадрах WebSocket. Хватает карт со строковыми
// ключами, строк, чисел, bool, nil и bin — отдельная зависимость ради этого не нужна.

func msgpackEncode(v any) ([]byte, error) { return mpAppend(nil, v) }

func mpAppend(b []byte, v any) ([]byte, error) {
	switch x := v.(type) {
	case nil:
		return append(b, 0xc0), nil
	case bool:
		if x {
			return append(b, 0xc3), nil
		}
		return append(b, 0xc2), nil
	case int:
		return binary.BigEndian.AppendUint32(append(b, 0xd2), uint32(int32(x))), nil
	case float64:
		return binary.BigEndian.AppendUint64(append(b, 0xcb), math.Float64bits(x)), nil
	case string:
		switch n := len(x); {
		case n < 32:
			b = append(b, 0xa0|byte(n))
		case n < 1<<8:
			b = append(b, 0xd9, byte(n))
		case n < 1<<16:
			b = binary.BigEndian.AppendUint16(append(b, 0xda), uint16(n))
		default:
			b = binary.BigEndian.AppendUint32(append(b, 0xdb), uint32(n))
		}
		return append(b, x...), nil
	case []byte:
		b = binary.BigEndian.AppendUint32(append(b, 0xc6), uint32(len(x)))
		return append(b, x...), nil
	case map[string]any:
		if len(x) < 16 {
			b = append(b, 0x80|byte(len(x)))
		} else {
			b = binary.BigEndian.AppendUint16(append(b, 0xde), uint16(len(x)))
		}
		var err error
		for k, e := range x {
			if b, err = mpAppend(b, k); err != nil {
				return nil, err
			}
			if b, err = mpAppend(b, e); err != nil {
				return nil, err
			}
		}
		return b, nil
	}
	return nil, errors.New("msgpack: неподдерживаемый тип")
}

func msgpackDecode(b []byte) (any, error) {
	v, rest, err := mpRead(b, 0)
	if err == nil && len(rest) != 0 {
		err = errors.New("msgpack: лишние данные")
	}
	return v, err
}

var errMsgpack = errors.New("msgpack: неверные данные")

func mpTake(b []byte, n int) (head, rest []byte, err error) {
	if n < 0 || len(b) < n {
		return nil, nil, errMsgpack
	}
	return b[:n], b[n:], nil
}

func mpRead(b []byte, depth int) (any, []byte, error) {
	if len(b) == 0 || depth > 16 {
		return nil, nil, errMsgpack
	}
	t, b := b[0], b[1:]
	size := func(n int) (int, error) { // длина из n байт
		h, _, err := mpTake(b, n)
		if err != nil {
			return 0, err
		}
		v := 0
		for _, c := range h {
			v = v<<8 | int(c)
		}
		if v < 0 || v > 64<<20 {
			return 0, errMsgpack
		}
		b = b[n:]
		return v, nil
	}
	num := func(n int, signed bool) (any, []byte, error) {
		h, rest, err := mpTake(b, n)
		if err != nil {
			return nil, nil, err
		}
		var u uint64
		for _, c := range h {
			u = u<<8 | uint64(c)
		}
		if signed {
			return int64(u<<(64-8*uint(n))) >> (64 - 8*uint(n)), rest, nil
		}
		return int64(u), rest, nil
	}
	str := func(n int, raw bool) (any, []byte, error) {
		h, rest, err := mpTake(b, n)
		if err != nil {
			return nil, nil, err
		}
		if raw {
			return append([]byte(nil), h...), rest, nil
		}
		return string(h), rest, nil
	}
	collection := func(n int, isMap bool) (any, []byte, error) {
		if n > 1<<16 {
			return nil, nil, errMsgpack
		}
		m, arr := map[string]any{}, make([]any, 0, n)
		var err error
		for i := 0; i < n; i++ {
			var k, e any
			if isMap {
				if k, b, err = mpRead(b, depth+1); err != nil {
					return nil, nil, err
				}
			}
			if e, b, err = mpRead(b, depth+1); err != nil {
				return nil, nil, err
			}
			if isMap {
				ks, ok := k.(string)
				if !ok {
					return nil, nil, errMsgpack
				}
				m[ks] = e
			} else {
				arr = append(arr, e)
			}
		}
		if isMap {
			return m, b, nil
		}
		return arr, b, nil
	}
	switch {
	case t <= 0x7f:
		return int64(t), b, nil
	case t >= 0xe0:
		return int64(int8(t)), b, nil
	case t&0xe0 == 0xa0:
		return str(int(t&0x1f), false)
	case t&0xf0 == 0x80:
		return collection(int(t&0x0f), true)
	case t&0xf0 == 0x90:
		return collection(int(t&0x0f), false)
	}
	switch t {
	case 0xc0:
		return nil, b, nil
	case 0xc2:
		return false, b, nil
	case 0xc3:
		return true, b, nil
	case 0xcc:
		return num(1, false)
	case 0xcd:
		return num(2, false)
	case 0xce:
		return num(4, false)
	case 0xcf:
		return num(8, false)
	case 0xd0:
		return num(1, true)
	case 0xd1:
		return num(2, true)
	case 0xd2:
		return num(4, true)
	case 0xd3:
		return num(8, true)
	case 0xca:
		h, rest, err := mpTake(b, 4)
		if err != nil {
			return nil, nil, err
		}
		return float64(math.Float32frombits(binary.BigEndian.Uint32(h))), rest, nil
	case 0xcb:
		h, rest, err := mpTake(b, 8)
		if err != nil {
			return nil, nil, err
		}
		return math.Float64frombits(binary.BigEndian.Uint64(h)), rest, nil
	case 0xd9, 0xda, 0xdb, 0xc4, 0xc5, 0xc6:
		w := map[byte]int{0xd9: 1, 0xda: 2, 0xdb: 4, 0xc4: 1, 0xc5: 2, 0xc6: 4}[t]
		n, err := size(w)
		if err != nil {
			return nil, nil, err
		}
		return str(n, t == 0xc4 || t == 0xc5 || t == 0xc6)
	case 0xdc, 0xdd, 0xde, 0xdf:
		w := map[byte]int{0xdc: 2, 0xdd: 4, 0xde: 2, 0xdf: 4}[t]
		n, err := size(w)
		if err != nil {
			return nil, nil, err
		}
		return collection(n, t >= 0xde)
	}
	return nil, nil, errMsgpack
}
