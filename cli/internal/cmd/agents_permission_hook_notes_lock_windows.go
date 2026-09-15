//go:build windows

package cmd

import (
	"os"
	"runtime"

	"golang.org/x/sys/windows"
)

func lockOperatorNoteFile(f *os.File) error {
	var overlapped windows.Overlapped
	err := windows.LockFileEx(
		windows.Handle(f.Fd()),
		windows.LOCKFILE_EXCLUSIVE_LOCK,
		0,
		1,
		0,
		&overlapped,
	)
	runtime.KeepAlive(f)
	return err
}

func unlockOperatorNoteFile(f *os.File) error {
	var overlapped windows.Overlapped
	err := windows.UnlockFileEx(windows.Handle(f.Fd()), 0, 1, 0, &overlapped)
	runtime.KeepAlive(f)
	return err
}
