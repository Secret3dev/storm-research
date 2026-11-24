#!/usr/bin/env python3
"""smi-storm-guard: end an SMI storm on a TDX host by pausing the guest that causes it.

Runs as root on the host. It reads the SMI count (MSR 0x34) once a second.
When the rate stays high it pauses the running TDX guests one at a time. The
guest whose pause makes the rate fall is the one in the loop. That guest is
left paused ("parked") and every other guest runs on.

It never kills, stops or restarts a guest. See README.md for why.
"""

from __future__ import annotations

import json
import os
import re
import signal
import struct
import subprocess
import sys
import time
from dataclasses import dataclass, field


def _number(name: str, default: float) -> float:
    raw = os.environ.get(name, "")
    if raw == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        raise SystemExit(f"{name} must be a number, not {raw!r}")
    if value <= 0:
        raise SystemExit(f"{name} must be above 0, not {raw!r}")
    return value


def _pattern(name: str, default: str) -> "re.Pattern[str]":
    raw = os.environ.get(name, default)
    try:
        return re.compile(raw)
    except re.error as e:
        raise SystemExit(f"{name} is not a valid regular expression: {e}")


SMI_COUNT_MSR = 0x34

STORM_PER_SECOND = _number("GUARD_STORM_RATE", 200)   # a storm reads 1,500 to 2,100; a quiet host reads 0
CALM_PER_SECOND = _number("GUARD_CALM_RATE", 20)      # below this the storm is over
STORM_SECONDS = int(_number("GUARD_STORM_SECONDS", 3))  # the rate must stay high this long before a pause
MAX_PARKS = int(_number("GUARD_MAX_PARKS", 3))        # guests parked in PARK_WINDOW before it only watches
PARK_WINDOW = 3600
SETTLE_SECONDS = 0.3   # wait after pausing a guest before measuring
PROBE_SECONDS = 1.0    # how long the rate is measured with a guest paused

# Which processes are TDX guests. BINARY is matched against the file name of
# the program, ARGS against the rest of its command line.
BINARY = _pattern("GUARD_BINARY", r"^qemu-system-x86")
ARGS = _pattern("GUARD_ARGS", r"tdx-guest")

CPU = os.environ.get("GUARD_CPU", "0")   # the CPU whose SMI count is read
if not CPU.isdigit():
    raise SystemExit(f"GUARD_CPU must be a CPU number, not {CPU!r}")
CPU = int(CPU)
OFF_FILE = os.environ.get("GUARD_OFF_FILE", "/etc/smi-storm-guard/off")
STATE_DIR = os.environ.get("GUARD_STATE_DIR", "/run/smi-storm-guard")   # under /run: a reboot un-parks
LOG_DIR = os.environ.get("GUARD_LOG_DIR", "/var/log/smi-storm-guard")
PARKED_FILE = os.path.join(STATE_DIR, "parked.json")
PROBING_FILE = os.path.join(STATE_DIR, "probing.json")


@dataclass(frozen=True)
class Guest:
    pid: int
    start: int   # start time from /proc/<pid>/stat; tells a reused pid apart
    name: str

    @property
    def key(self) -> str:
        return f"{self.pid}:{self.start}"


def guest_name(argv: list[str], pid: int) -> str:
    """The name QEMU was given with -name, or the pid."""
    for i, arg in enumerate(argv[:-1]):
        if arg != "-name":
            continue
        parts = argv[i + 1].split(",")
        for part in parts:
            if part.startswith("guest="):
                return part[len("guest="):]
        if parts and "=" not in parts[0]:
            return parts[0]
    return f"pid{pid}"


class Host:
    """Everything the guard reads from or does to the machine."""

    def __init__(self, proc: str = "/proc", cpu: int = CPU):
        self.proc = proc
        self.cpu = cpu
        self._msr = None

    def smi_count(self) -> int:
        if self._msr is None:
            self._msr = os.open(f"/dev/cpu/{self.cpu}/msr", os.O_RDONLY)
        return struct.unpack("<Q", os.pread(self._msr, 8, SMI_COUNT_MSR))[0]

    def now(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def _stat(self, pid: int) -> tuple[str, int]:
        """(state, start time) of a process."""
        with open(f"{self.proc}/{pid}/stat") as f:
            fields = f.read().rsplit(")", 1)[1].split()
        return fields[0], int(fields[19])

    def guests(self) -> list[Guest]:
        found = []
        for entry in os.listdir(self.proc):
            if not entry.isdigit():
                continue
            pid = int(entry)
            try:
                with open(f"{self.proc}/{entry}/cmdline", "rb") as f:
                    argv = [a.decode(errors="replace") for a in f.read().split(b"\0") if a]
                if not argv or not BINARY.search(os.path.basename(argv[0])):
                    continue
                if not ARGS.search(" ".join(argv[1:])):
                    continue
                state, start = self._stat(pid)
            except (OSError, IndexError, ValueError):
                continue   # the process ended while it was being read
            if state in ("Z", "X"):
                continue
            found.append(Guest(pid, start, guest_name(argv, pid)))
        return sorted(found, key=lambda g: g.pid)

    def is_paused(self, guest: Guest) -> bool:
        try:
            state, start = self._stat(guest.pid)
        except (OSError, IndexError, ValueError):
            return False
        return start == guest.start and state == "T"

    def _signal(self, guest: Guest, sig: int) -> None:
        try:
            _, start = self._stat(guest.pid)
        except (OSError, IndexError, ValueError):
            raise ProcessLookupError(guest.pid)
        if start != guest.start:   # the pid now belongs to another process
            raise ProcessLookupError(guest.pid)
        os.kill(guest.pid, sig)

    def pause(self, guest: Guest) -> None:
        self._signal(guest, signal.SIGSTOP)

    def resume(self, guest: Guest) -> None:
        try:
            self._signal(guest, signal.SIGCONT)
        except ProcessLookupError:
            pass

    def _read(self, path: str) -> dict:
        try:
            with open(path) as f:
                got = json.load(f)
            return got if isinstance(got, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(self, path: str, data: dict) -> None:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    def parked(self) -> dict[str, str]:
        """key -> name of the guests this guard parked since boot."""
        return {str(k): str(v) for k, v in self._read(PARKED_FILE).items()}

    def remember_parked(self, guest: Guest) -> None:
        now = self.parked()
        now[guest.key] = guest.name
        self._write(PARKED_FILE, now)

    def probing(self) -> dict[str, str]:
        """key -> name of the guests a search is pausing right now."""
        return {str(k): str(v) for k, v in self._read(PROBING_FILE).items()}

    def remember_probing(self, guests: list[Guest]) -> None:
        self._write(PROBING_FILE, {g.key: g.name for g in guests})

    def acting_allowed(self) -> bool:
        return not os.path.exists(OFF_FILE)

    def log(self, message: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())} UTC {message}"
        print(line, flush=True)
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(os.path.join(LOG_DIR, "guard.log"), "a") as f:
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())   # the host may be gone a second later; the line must not be
        except OSError:
            pass

    def save_evidence(self, guest: Guest, rate: float) -> None:
        """What a person will want afterwards: which threads were spinning, and the kernel log."""
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        path = os.path.join(LOG_DIR, f"{stamp}-{guest.pid}.txt")
        parts = [f"guest {guest.name} pid {guest.pid} SMI rate {rate:.0f}/s\n"]
        for title, cmd in (
            ("threads", ["ps", "-L", "-o", "tid,stat,psr,pcpu,time,comm", "-p", str(guest.pid)]),
            ("kernel", ["journalctl", "-k", "--since", "-5min", "--no-pager", "-o", "short-precise"]),
        ):
            try:
                got = subprocess.run(cmd, capture_output=True, text=True, timeout=20, errors="replace").stdout
            except (OSError, subprocess.SubprocessError) as e:
                got = f"could not read: {e}\n"
            parts.append(f"\n== {title}\n{got[-20000:]}")
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(path, "w") as f:
                f.write("".join(parts))
                f.flush()
                os.fsync(f.fileno())
        except OSError:
            pass

    def publish(self, status: dict) -> None:
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            tmp = os.path.join(STATE_DIR, "status.json.tmp")
            with open(tmp, "w") as f:
                json.dump(status, f)
            os.replace(tmp, os.path.join(STATE_DIR, "status.json"))
        except OSError:
            pass


def rate_over(host, seconds: float) -> float:
    """SMIs per second on one CPU, measured over `seconds`."""
    c0, t0 = host.smi_count(), host.now()
    host.sleep(seconds)
    c1, t1 = host.smi_count(), host.now()
    return (c1 - c0) / max(t1 - t0, 1e-6)


def find_culprits(host, guests: list[Guest]) -> tuple[list[Guest], str]:
    """Which of `guests` keep the storm going. They are left paused; every other guest is running again.

    First each guest is paused alone. If no single pause ends the storm, all are
    paused together; if that ends it, they are resumed one at a time and each
    guest whose resume brings the storm back is paused again.
    """
    for g in guests:
        try:
            host.pause(g)
        except ProcessLookupError:
            continue
        host.sleep(SETTLE_SECONDS)
        if rate_over(host, PROBE_SECONDS) < CALM_PER_SECOND:
            return [g], "pausing it alone ended the storm"
        host.resume(g)

    paused = []
    for g in guests:
        try:
            host.pause(g)
            paused.append(g)
        except ProcessLookupError:
            pass
    host.sleep(SETTLE_SECONDS)
    if rate_over(host, PROBE_SECONDS) >= CALM_PER_SECOND:
        for g in paused:
            host.resume(g)
        return [], "the storm goes on with every TDX guest paused, so no TDX guest is the cause"

    culprits = []
    for g in paused:
        host.resume(g)
        host.sleep(SETTLE_SECONDS)
        if rate_over(host, PROBE_SECONDS) >= CALM_PER_SECOND:
            host.pause(g)
            host.sleep(SETTLE_SECONDS)
            culprits.append(g)
    return culprits, "more than one guest was in the loop"


@dataclass
class Guard:
    host: object
    high_for: int = 0
    parks: list[float] = field(default_factory=list)
    tripped: bool = False
    last: str = "started"

    def recent_parks(self) -> int:
        cutoff = self.host.now() - PARK_WINDOW
        self.parks = [t for t in self.parks if t >= cutoff]
        return len(self.parks)

    def step(self) -> float:
        """One pass: measure a second, and act if a storm has lasted long enough."""
        host = self.host
        rate = rate_over(host, 1.0)
        self.high_for = self.high_for + 1 if rate >= STORM_PER_SECOND else 0
        host.publish({"rate": round(rate), "high_for": self.high_for, "tripped": self.tripped,
                      "acting": host.acting_allowed(), "last": self.last})
        if self.high_for < STORM_SECONDS:
            return rate

        self.high_for = 0
        if not host.acting_allowed():
            self._say(f"SMI storm, {rate:.0f}/s. {OFF_FILE} exists, so only watching")
            host.sleep(30)
            return rate
        if self.tripped or self.recent_parks() >= MAX_PARKS:
            if not self.tripped:
                self.tripped = True
                self._say(f"SMI storm, {rate:.0f}/s, and {MAX_PARKS} guests were already parked this hour. "
                          "Only watching from here; restart the guard to let it act again")
            host.sleep(30)
            return rate

        parked = host.parked()
        again = [g for g in host.guests() if g.key in parked]
        if again:
            for g in again:   # something may have resumed a parked guest; pausing twice is harmless
                try:
                    host.pause(g)
                except ProcessLookupError:
                    pass
            host.sleep(SETTLE_SECONDS)
            if rate_over(host, PROBE_SECONDS) < CALM_PER_SECOND:
                self._say(f"SMI storm, {rate:.0f}/s, ended by pausing the parked guests again: "
                          f"{', '.join(g.name for g in again)}")
                return 0.0
        # A guest someone else paused cannot be in the loop, and its pause is not the guard's to undo.
        guests = [g for g in host.guests() if g.key not in parked and not host.is_paused(g)]
        if not guests:
            self._say(f"SMI storm, {rate:.0f}/s, and no TDX guest is running that is not already paused. "
                      "Nothing to do")
            host.sleep(30)
            return rate

        self._say(f"SMI storm, {rate:.0f}/s. Pausing {len(guests)} TDX guests in turn to find the one in the loop")
        host.remember_probing(guests)   # first, so a restarted guard knows which pauses were its own
        try:
            culprits, why = find_culprits(host, guests)
        except BaseException:
            for g in guests:   # never leave a guest paused because the search itself failed
                host.resume(g)
            host.remember_probing([])
            raise
        if not culprits:
            host.remember_probing([])
            self._say(f"No guest parked: {why}")
            host.sleep(30)
            return rate

        for g in culprits:
            host.remember_parked(g)   # before the probing list is cleared, so a restart never resumes it
            self.parks.append(host.now())
            self._say(f"Parked {g.name} (pid {g.pid}): {why}. It stays paused until the host is rebooted. "
                      "Do not resume, stop, restart or kill it")
        host.remember_probing([])
        for g in culprits:
            host.save_evidence(g, rate)

        after = rate_over(host, 1.0)
        if after < CALM_PER_SECOND:
            self._say(f"Storm ended, {after:.0f}/s. Parked now: {', '.join(sorted(host.parked().values()))}")
        else:
            self._say(f"Storm did NOT end with {len(culprits)} guest(s) parked, still {after:.0f}/s")
        return after

    def _say(self, message: str) -> None:
        self.last = message
        self.host.log(message)


class DrillHost(Host):
    """A rehearsal on the real machine. The SMI count is made up: it climbs at a storm's rate
    for as long as one chosen guest is running and not paused. The pausing the guard does in
    answer is real. The rehearsal resumes the guest it parked and forgets it."""

    def __init__(self, pid: int):
        super().__init__()
        match = [g for g in self.guests() if g.pid == pid]
        if not match:
            raise SystemExit(f"drill: pid {pid} is not a running TDX guest (see --list)")
        self.target = match[0]
        self.drill_parked: dict[str, str] = {}
        self._count = 0
        self._at = time.monotonic()

    def smi_count(self) -> int:
        now = time.monotonic()
        try:
            state, start = self._stat(self.target.pid)
            running = start == self.target.start and state not in ("T", "Z", "X")
        except (OSError, IndexError, ValueError):
            running = False
        if running:
            self._count += int(2100 * (now - self._at))
        self._at = now
        return self._count

    def acting_allowed(self) -> bool:
        return True

    def remember_parked(self, guest: Guest) -> None:   # a rehearsal leaves no parked record behind
        self.drill_parked[guest.key] = guest.name

    def parked(self) -> dict[str, str]:
        return dict(self.drill_parked)

    def remember_probing(self, guests: list[Guest]) -> None:
        pass


def drill(pid: int) -> int:
    host = DrillHost(pid)
    guard = Guard(host)
    watched = [g for g in host.guests() if not host.is_paused(g)]
    host.log(f"DRILL: pretending {host.target.name} (pid {host.target.pid}) is in the loop. "
             "The SMI count is made up; the pause is real")
    deadline = time.monotonic() + 120
    try:
        while time.monotonic() < deadline and not guard.parks:
            guard.step()
    finally:
        real = Host().parked()
        for g in watched:   # a rehearsal leaves every guest running, except one that is really parked
            if g.key not in real:
                host.resume(g)
    ok = (bool(guard.parks) and guard.last.startswith("Storm ended")
          and host.parked() == {host.target.key: host.target.name})
    host.log("DRILL: " + ("passed; the guest was resumed" if ok else f"FAILED, last message: {guard.last}"))
    return 0 if ok else 1


def show(host: Host) -> int:
    guests = host.guests()
    parked = host.parked()
    if not guests:
        print("no running process matches GUARD_BINARY and GUARD_ARGS")
    for g in guests:
        state = "parked" if g.key in parked else ("paused" if host.is_paused(g) else "running")
        print(f"{g.pid}\t{state}\t{g.name}")
    try:
        print(f"SMI rate on CPU {host.cpu}: {rate_over(host, 1.0):.0f}/s")
    except OSError as e:
        print(f"SMI rate not read: {e}. Run as root with the msr module loaded")
    return 0


def resume_interrupted_search(host: Host) -> None:
    """A guard that was stopped in the middle of a search may have left a guest paused.
    Only guests that search paused are resumed. A parked guest stays parked."""
    probing = host.probing()
    parked = host.parked()
    for g in host.guests():
        if g.key in parked:
            host.log(f"{g.name} is parked (pid {g.pid}); leaving it paused")
        elif g.key in probing and host.is_paused(g):
            host.resume(g)
            host.log(f"{g.name} was left paused by an interrupted search; resumed it")
    if probing:
        host.remember_probing([])


USAGE = """usage: smi-storm-guard               watch and act
       smi-storm-guard --list        show the guests it would watch and the SMI rate
       smi-storm-guard --drill PID   rehearse on one running guest; it is paused, then resumed"""


def main() -> int:
    if sys.argv[1:] == ["--list"]:
        return show(Host())
    if len(sys.argv) == 3 and sys.argv[1] == "--drill" and sys.argv[2].isdigit():
        return drill(int(sys.argv[2]))
    if len(sys.argv) != 1:
        print(USAGE, file=sys.stderr)
        return 2
    host = Host()
    host.smi_count()   # fail now, not in the loop, if the MSR cannot be read
    guard = Guard(host)
    resume_interrupted_search(host)
    host.log("watching the SMI count" + ("" if host.acting_allowed() else f" ({OFF_FILE} exists: not acting)"))
    while True:
        try:
            guard.step()
        except Exception as e:   # keep watching; a guard that died protects nothing
            host.log(f"error in one pass: {type(e).__name__}: {e}")
            time.sleep(5)


if __name__ == "__main__":
    sys.exit(main())
