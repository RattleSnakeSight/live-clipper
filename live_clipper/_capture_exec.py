"""Replace this Linux child with a capture reader tied to the spawning worker."""
import ctypes
import os
import signal
import sys

if __name__ == "__main__":
    expected_parent = int(sys.argv[1])
    command = sys.argv[2:]
    prctl = ctypes.CDLL(None, use_errno=True).prctl
    PR_SET_PDEATHSIG = 1
    if prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    # Death before prctl would have sent no signal. Never start a reader in that case.
    if os.getppid() != expected_parent:
        sys.exit(1)
    os.execv(command[0], command)
