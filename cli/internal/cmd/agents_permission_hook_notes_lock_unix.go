//go:build !windows

package cmd

import (
	"os"
	"runtime"

	"golang.org/x/sys/unix"
)

func lockOperatorNoteFile(f *os.File) error {
	err := unix.Flock(int(f.Fd()), unix.LOCK_EX)
	runtime.KeepAlive(f)
	return err
}

func unlockOperatorNoteFile(f *os.File) error {
	err := unix.Flock(int(f.Fd()), unix.LOCK_UN)
	runtime.KeepAlive(f)
	return err
}
