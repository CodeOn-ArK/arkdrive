"""Detach a destination-session selection holder; stdin is a seekable payload.

The controller waits only for this launcher. The orphaned holder has its own
POSIX session, no controller pipes, and sessiond's exact environment markers.
It serves unlimited reads until ownership is replaced, X exits, or sessiond
reaps the destination. Read-back, never launcher exit, proves installation.
"""
import os


def main():
    if os.fork():
        return
    os.setsid()
    # -quiet keeps this process in the foreground (no additional xclip fork).
    # Zero loops means unlimited requests: verification must not consume it.
    os.execvp('xclip', ['xclip', '-selection', 'clipboard', '-in', '-quiet', '-loops', '0'])


if __name__ == '__main__':
    main()
