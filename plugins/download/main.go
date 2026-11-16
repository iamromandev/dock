// download plugin for dock, written against the raw protocol (stdlib only).
// JSON-RPC 2.0, one JSON object per line on stdin/stdout. See protocol/PROTOCOL.md.
package main

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path"
	"path/filepath"
	"sync"
	"time"
)

type rpcError struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
}

type message struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      any             `json:"id,omitempty"`
	Method  string          `json:"method,omitempty"`
	Params  json.RawMessage `json:"params,omitempty"`
	Result  any             `json:"result,omitempty"`
	Error   *rpcError       `json:"error,omitempty"`
}

var outMu sync.Mutex

func send(m message) {
	m.JSONRPC = "2.0"
	b, _ := json.Marshal(m)
	outMu.Lock()
	defer outMu.Unlock()
	os.Stdout.Write(append(b, '\n'))
}

func notify(method string, params any) {
	raw, _ := json.Marshal(params)
	send(message{Method: method, Params: raw})
}

func output(text string) { notify("output", map[string]string{"text": text}) }
func progress(pct float64, msg string) {
	notify("progress", map[string]any{"percent": pct, "message": msg})
}

func main() {
	in := bufio.NewScanner(os.Stdin)
	in.Buffer(make([]byte, 1024*1024), 16*1024*1024)

	var cancel context.CancelFunc = func() {}
	var running sync.WaitGroup

	for in.Scan() {
		var m message
		if err := json.Unmarshal(in.Bytes(), &m); err != nil || m.Method == "" {
			continue
		}
		switch m.Method {
		case "initialize":
			send(message{ID: m.ID, Result: map[string]string{"name": "download"}})
		case "cancel": // notification from Ctrl-C
			cancel()
		case "invoke":
			var p struct {
				Command string   `json:"command"`
				Args    []string `json:"args"`
				Cwd     string   `json:"cwd"`
			}
			json.Unmarshal(m.Params, &p)
			var ctx context.Context
			ctx, cancel = context.WithCancel(context.Background())
			running.Add(1)
			go func(id any) { // run off the read loop so we can still receive `cancel`
				defer running.Done()
				if p.Command != "download" {
					send(message{ID: id, Error: &rpcError{-32601, "unknown command " + p.Command}})
					return
				}
				if err := download(ctx, p.Args, p.Cwd); err != nil {
					code := -32000
					if errors.Is(err, context.Canceled) {
						code = -32800
					}
					send(message{ID: id, Error: &rpcError{code, err.Error()}})
					return
				}
				send(message{ID: id, Result: map[string]int{"exit_code": 0}})
			}(m.ID)
		case "shutdown":
			cancel()
			running.Wait()
			send(message{ID: m.ID, Result: map[string]any{}})
			return
		default:
			if m.ID != nil {
				send(message{ID: m.ID, Error: &rpcError{-32601, "unknown method " + m.Method}})
			}
		}
	}
}

func download(ctx context.Context, args []string, cwd string) error {
	var src, out string
	for i := 0; i < len(args); i++ {
		switch a := args[i]; {
		case a == "-o" || a == "--output":
			if i+1 >= len(args) {
				return fmt.Errorf("-o needs a file name")
			}
			i++
			out = args[i]
		case src == "":
			src = a
		default:
			return fmt.Errorf("unexpected argument %q", a)
		}
	}
	if src == "" {
		return fmt.Errorf("usage: download <url> [-o file]")
	}
	u, err := url.Parse(src)
	if err != nil {
		return err
	}
	if out == "" {
		out = path.Base(u.Path)
		if out == "" || out == "/" || out == "." {
			out = "download.out"
		}
	}
	if !filepath.IsAbs(out) && cwd != "" {
		out = filepath.Join(cwd, out) // the user's directory, not ours
	}

	body, size, err := open(ctx, u)
	if err != nil {
		return err
	}
	defer body.Close()

	tmp := out + ".part"
	f, err := os.Create(tmp)
	if err != nil {
		return err
	}
	n, copyErr := copyWithProgress(ctx, f, body, size)
	f.Close()
	if copyErr != nil {
		os.Remove(tmp)
		return copyErr
	}
	if err := os.Rename(tmp, out); err != nil {
		return err
	}
	abs, _ := filepath.Abs(out)
	output(fmt.Sprintf("saved %s (%d bytes)\n", abs, n))
	return nil
}

func open(ctx context.Context, u *url.URL) (io.ReadCloser, int64, error) {
	switch u.Scheme {
	case "file":
		f, err := os.Open(u.Path)
		if err != nil {
			return nil, 0, err
		}
		st, _ := f.Stat()
		return f, st.Size(), nil
	case "http", "https":
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, u.String(), nil)
		if err != nil {
			return nil, 0, err
		}
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			return nil, 0, err
		}
		if resp.StatusCode >= 400 {
			resp.Body.Close()
			return nil, 0, fmt.Errorf("server said %s", resp.Status)
		}
		return resp.Body, resp.ContentLength, nil
	}
	return nil, 0, fmt.Errorf("unsupported scheme %q", u.Scheme)
}

func copyWithProgress(ctx context.Context, w io.Writer, r io.Reader, size int64) (int64, error) {
	buf := make([]byte, 64*1024)
	var done int64
	last := time.Time{}
	for {
		if err := ctx.Err(); err != nil {
			return done, err
		}
		n, err := r.Read(buf)
		if n > 0 {
			if _, werr := w.Write(buf[:n]); werr != nil {
				return done, werr
			}
			done += int64(n)
			if time.Since(last) > 100*time.Millisecond {
				last = time.Now()
				if size > 0 {
					progress(float64(done)*100/float64(size), "downloading")
				} else {
					notify("progress", map[string]any{"percent": nil, "message": fmt.Sprintf("%d bytes", done)})
				}
			}
		}
		if err == io.EOF {
			return done, nil
		}
		if err != nil {
			return done, err
		}
	}
}
