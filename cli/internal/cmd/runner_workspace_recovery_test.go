package cmd

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/preloop/preloop/cli/internal/testenv"
)

// A continuation that lands on a runner without the workspace used to start
// from a cold clone, which silently discards the unpushed commits it was
// leased to continue (preloop/preloop#386, required behavior 4 and 6).
func TestResumeWithoutLocalWorkspaceIsExplicit(t *testing.T) {
	testenv.SetTempHome(t)
	prior := "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
	current := "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"

	dest, recovered, err := preparePersistWorkspace(current, prior)
	if err != nil {
		t.Fatal(err)
	}
	if recovered {
		t.Fatal("a workspace this host never held must not report as recovered")
	}
	reason := workspaceRecoveryUnavailable(prior, dest).Error()
	for _, want := range []string{
		"workspace_recovery_unavailable",
		"no local workspace",
		prior,
		dest,
		"never uploaded",
	} {
		if !strings.Contains(reason, want) {
			t.Fatalf("reason %q is missing %q", reason, want)
		}
	}
}

// Retention and quota loss reads differently from "wrong host": the local
// tombstone says which one happened.
func TestExpiredWorkspaceReportsTheRecordedLoss(t *testing.T) {
	testenv.SetTempHome(t)
	prior := "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
	current := "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
	priorDir, err := runnerWorkspaceDir(prior)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Dir(priorDir), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(priorDir+".expired", []byte("workspace_quota_exceeded\n"), 0o600); err != nil {
		t.Fatal(err)
	}

	dest, recovered, err := preparePersistWorkspace(current, prior)
	if err != nil {
		t.Fatal(err)
	}
	if recovered {
		t.Fatal("an expired workspace is not recovered state")
	}
	reason := workspaceRecoveryUnavailable(prior, dest).Error()
	if !strings.Contains(reason, "workspace_quota_exceeded") {
		t.Fatalf("reason %q does not report the recorded loss", reason)
	}
}

// A first run has nothing to recover and must not be refused.
func TestFreshWorkspaceWithoutResumeIsNotARecoveryFailure(t *testing.T) {
	testenv.SetTempHome(t)
	current := "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
	if _, recovered, err := preparePersistWorkspace(current, ""); err != nil || !recovered {
		t.Fatalf("recovered = %v, err = %v", recovered, err)
	}
}

// Re-leasing the same execution (a reconnect, or a redispatch after the
// runner process died mid-execution) keeps its own directory and must not
// read as a missing workspace.
func TestReleasingTheSameExecutionKeepsItsOwnWorkspace(t *testing.T) {
	testenv.SetTempHome(t)
	current := "ffffffff-ffff-4fff-8fff-ffffffffffff"
	dir, err := runnerWorkspaceDir(current)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "notes.txt"), []byte("mid-run"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := touchWorkspaceLease(current); err != nil {
		t.Fatal(err)
	}

	dest, recovered, err := preparePersistWorkspace(current, current)
	if err != nil {
		t.Fatal(err)
	}
	if !recovered {
		t.Fatal("the execution's own surviving workspace is recovery state")
	}
	if body, err := os.ReadFile(filepath.Join(dest, "notes.txt")); err != nil || string(body) != "mid-run" {
		t.Fatalf("body = %q, err = %v", body, err)
	}
}

// Runner bookkeeping alone is not recovered work: a swept directory that
// only carries a lease file must still refuse the resume.
func TestLeaseFileAloneIsNotRecoveredWork(t *testing.T) {
	testenv.SetTempHome(t)
	current := "12121212-1212-4121-8121-121212121212"
	dir, err := runnerWorkspaceDir(current)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := touchWorkspaceLease(current); err != nil {
		t.Fatal(err)
	}
	if _, recovered, err := preparePersistWorkspace(current, current); err != nil || recovered {
		t.Fatalf("recovered = %v, err = %v", recovered, err)
	}
}
