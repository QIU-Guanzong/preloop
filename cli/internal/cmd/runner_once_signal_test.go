//go:build !windows

package cmd

import (
	"bytes"
	"os"
	"os/signal"
	"syscall"
	"testing"
	"time"

	"github.com/preloop/preloop/cli/internal/testenv"
)

// This case lives in a !windows file: syscall.Kill does not exist on
// Windows, so keeping it in the shared test file breaks `go vet` and the
// build for GOOS=windows even though the case itself was skipped there.

func TestRunnerFgEphemeralUnregistersOnSigterm(t *testing.T) {
	testenv.SetTempHome(t)
	// Disarm the default action for the whole process before anything can
	// deliver SIGTERM, so a mistimed signal cannot kill the test binary.
	guard := make(chan os.Signal, 1)
	signal.Notify(guard, syscall.SIGTERM)
	t.Cleanup(func() { signal.Stop(guard) })

	state, server := newOnceServer(t, nil)
	oldToken, oldURL := FlagToken, FlagURL
	FlagURL, FlagToken = server.URL, "tok"
	t.Cleanup(func() { FlagToken, FlagURL = oldToken, oldURL })

	done := make(chan error, 1)
	var out bytes.Buffer
	go func() {
		done <- runFgFlags(t, &out, map[string]string{
			"once": "true", "ephemeral": "true", "wait-for-job": "30s",
		})
	}()

	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if _, _, _, connections := state.snapshot(); connections > 0 {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	// The foreground loop is connected; ask it to stop the way a cancelled
	// CI job does.
	if err := syscall.Kill(os.Getpid(), syscall.SIGTERM); err != nil {
		t.Fatalf("kill: %v", err)
	}

	select {
	case err := <-done:
		if got := ProcessExitCode(err); got != runnerOnceExitNoJob {
			t.Fatalf("exit code = %d, want %d (%v)", got, runnerOnceExitNoJob, err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("runner did not exit on SIGTERM")
	}
	if _, _, unregisters, _ := state.snapshot(); unregisters == 0 {
		t.Fatal("SIGTERM left the ephemeral runner registered")
	}
}
