"""Tests for smi-storm-guard.py. Run: python3 test_guard.py

Nothing here touches a machine. The host is a model: a clock, an SMI count that
climbs while a guest "in the loop" is running and not paused, and a record of
what the guard did. The last class reads a made-up /proc folder.
"""

import importlib.util
import os
import signal
import sys
import tempfile
import unittest

_spec = importlib.util.spec_from_file_location(
    "smi_storm_guard", os.path.join(os.path.dirname(os.path.abspath(__file__)), "smi-storm-guard.py"))
guard_mod = importlib.util.module_from_spec(_spec)
sys.modules["smi_storm_guard"] = guard_mod   # dataclasses looks the module up by name
_spec.loader.exec_module(guard_mod)
Guest, Guard, Host = guard_mod.Guest, guard_mod.Guard, guard_mod.Host

NAMES = ["api", "build", "db", "edge", "web"]


class ModelHost:
    def __init__(self, names=NAMES, looping=(), outside_storm=False, acting=True):
        self.clock = 0.0
        self.count = 0.0
        self.alive = {n: Guest(1000 + i, 5000 + i, n) for i, n in enumerate(names)}
        self.looping = set(looping)          # guest names that keep a storm going while they run
        self.outside_storm = outside_storm   # a storm no TDX guest causes
        self.paused = set()                  # pids
        self.acting = acting
        self.parked_map = {}
        self.probing_map = {}
        self.parks, self.pauses, self.logs, self.evidence = [], [], [], []
        self.vanish_on_pause = set()
        self.fail_rate_after = None

    def _storming(self):
        return self.outside_storm or any(
            n in self.alive and self.alive[n].pid not in self.paused for n in self.looping)

    def smi_count(self):
        if self.fail_rate_after is not None:
            self.fail_rate_after -= 1
            if self.fail_rate_after < 0:
                raise OSError("msr read failed")
        return int(self.count)

    def now(self):
        return self.clock

    def sleep(self, seconds):
        if self._storming():
            self.count += 2100 * seconds
        self.clock += seconds

    def guests(self):
        return sorted(self.alive.values(), key=lambda g: g.pid)

    def is_paused(self, guest):
        return guest.pid in self.paused

    def pause(self, guest):
        if guest.name in self.vanish_on_pause:
            del self.alive[guest.name]
            raise ProcessLookupError()
        self.pauses.append(guest.name)
        self.paused.add(guest.pid)

    def resume(self, guest):
        self.paused.discard(guest.pid)

    def parked(self):
        return dict(self.parked_map)

    def remember_parked(self, guest):
        self.parks.append(guest.name)
        self.parked_map[guest.key] = guest.name

    def probing(self):
        return dict(self.probing_map)

    def remember_probing(self, guests):
        self.probing_map = {g.key: g.name for g in guests}

    def acting_allowed(self):
        return self.acting

    def log(self, message):
        self.logs.append(message)

    def save_evidence(self, guest, rate):
        self.evidence.append(guest.name)

    def publish(self, status):
        self.status = status

    def paused_names(self):
        return sorted(n for n, g in self.alive.items() if g.pid in self.paused)


def run(guard, passes):
    for _ in range(passes):
        guard.step()


class GuardTests(unittest.TestCase):
    def test_the_guard_has_no_way_to_kill_or_stop_a_guest(self):
        with open(guard_mod.__file__) as f:
            source = f.read()
        for word in ("SIGKILL", "SIGTERM", "systemctl", "virsh", "quit", "system_powerdown"):
            self.assertNotIn(word, source)

    def test_quiet_host_is_left_alone(self):
        host = ModelHost()
        run(Guard(host), 30)
        self.assertEqual(host.pauses, [])
        self.assertEqual(host.parks, [])

    def test_one_guest_in_the_loop_is_parked_and_the_rest_run_on(self):
        host = ModelHost(looping={"db"})
        guard = Guard(host)
        run(guard, 6)
        self.assertEqual(host.parks, ["db"])
        self.assertEqual(host.paused_names(), ["db"])
        self.assertEqual(host.evidence, ["db"])
        self.assertTrue(guard.last.startswith("Storm ended"))
        self.assertEqual(host.probing_map, {})

    def test_a_parked_guest_stays_parked_through_quiet_time(self):
        host = ModelHost(looping={"db"})
        run(Guard(host), 60)
        self.assertEqual(host.parks, ["db"])
        self.assertEqual(host.paused_names(), ["db"])

    def test_a_storm_shorter_than_three_seconds_is_ignored(self):
        host = ModelHost(looping={"db"})
        guard = Guard(host)
        run(guard, 2)
        host.looping.clear()
        run(guard, 10)
        self.assertEqual(host.pauses, [])

    def test_two_guests_in_the_loop_are_both_parked(self):
        host = ModelHost(looping={"api", "web"})
        run(Guard(host), 8)
        self.assertEqual(sorted(host.parks), ["api", "web"])
        self.assertEqual(host.paused_names(), ["api", "web"])

    def test_a_second_freeze_parks_the_second_guest_and_leaves_the_first_parked(self):
        host = ModelHost(looping={"db"})
        guard = Guard(host)
        run(guard, 6)
        host.looping.add("edge")
        run(guard, 8)
        self.assertEqual(host.parks, ["db", "edge"])
        self.assertEqual(host.paused_names(), ["db", "edge"])

    def test_a_parked_guest_that_was_resumed_is_paused_again(self):
        host = ModelHost(looping={"db"})
        guard = Guard(host)
        run(guard, 6)
        host.paused.clear()   # someone sent SIGCONT
        run(guard, 6)
        self.assertEqual(host.parks, ["db"])
        self.assertEqual(host.paused_names(), ["db"])
        self.assertIn("ended by pausing the parked guests again", " ".join(host.logs))

    def test_a_storm_no_tdx_guest_causes_parks_nothing(self):
        host = ModelHost(outside_storm=True)
        run(Guard(host), 6)
        self.assertEqual(host.parks, [])
        self.assertEqual(host.paused_names(), [])
        self.assertIn("no TDX guest is the cause", " ".join(host.logs))

    def test_the_off_file_means_watch_only(self):
        host = ModelHost(looping={"db"}, acting=False)
        run(Guard(host), 6)
        self.assertEqual(host.pauses, [])
        self.assertIn("only watching", " ".join(host.logs))

    def test_after_three_parks_in_an_hour_it_only_watches(self):
        host = ModelHost(looping={"api"})
        guard = Guard(host)
        for name in ("build", "db", "edge"):
            run(guard, 8)
            host.looping.add(name)
        run(guard, 8)
        self.assertEqual(len(host.parks), 3)
        self.assertTrue(guard.tripped)
        self.assertNotIn("edge", host.paused_names())

    def test_parks_older_than_an_hour_do_not_count(self):
        host = ModelHost(looping={"api"})
        guard = Guard(host)
        for name in ("build", "db"):
            run(guard, 8)
            host.looping.add(name)
        run(guard, 8)
        host.clock += 3700
        host.looping.add("edge")
        run(guard, 8)
        self.assertEqual(len(host.parks), 4)
        self.assertFalse(guard.tripped)

    def test_a_guest_that_exits_during_the_search_does_not_stop_it(self):
        host = ModelHost(looping={"web"})
        host.vanish_on_pause.add("api")
        run(Guard(host), 6)
        self.assertEqual(host.parks, ["web"])

    def test_no_running_guest_means_nothing_to_do(self):
        host = ModelHost(names=[], outside_storm=True)
        run(Guard(host), 6)
        self.assertEqual(host.pauses, [])
        self.assertIn("Nothing to do", " ".join(host.logs))

    def test_a_failed_search_leaves_no_guest_paused(self):
        host = ModelHost(looping={"web"})
        guard = Guard(host)
        run(guard, 2)
        host.fail_rate_after = 6   # the count stops being readable in the middle of the search
        with self.assertRaises(OSError):
            run(guard, 3)
        self.assertEqual(host.paused_names(), [])
        self.assertEqual(host.probing_map, {})

    def test_a_guest_someone_else_paused_is_left_as_it_is(self):
        host = ModelHost(looping={"db"})
        host.paused.add(host.alive["api"].pid)   # an operator paused it before the storm
        run(Guard(host), 6)
        self.assertNotIn("api", host.pauses)
        self.assertEqual(host.paused_names(), ["api", "db"])
        self.assertEqual(host.parks, ["db"])


class RestartTests(unittest.TestCase):
    def test_a_restarted_guard_leaves_a_parked_guest_paused(self):
        host = ModelHost(looping={"db"})
        run(Guard(host), 6)
        guard_mod.resume_interrupted_search(host)
        run(Guard(host), 10)
        self.assertEqual(host.parks, ["db"])
        self.assertEqual(host.paused_names(), ["db"])

    def test_a_restarted_guard_resumes_only_what_a_search_paused(self):
        host = ModelHost()
        api, web = host.alive["api"], host.alive["web"]
        host.paused.update({api.pid, web.pid})
        host.probing_map = {api.key: api.name}   # the search had paused api; an operator had paused web
        guard_mod.resume_interrupted_search(host)
        self.assertEqual(host.paused_names(), ["web"])
        self.assertEqual(host.probing_map, {})


def fake_process(root, pid, argv, state="S", start=4242, comm="qemu-system-x86"):
    folder = os.path.join(root, str(pid))
    os.makedirs(folder)
    with open(os.path.join(folder, "cmdline"), "wb") as f:
        f.write(b"\0".join(a.encode() for a in argv) + b"\0")
    fields = [state] + ["0"] * 18 + [str(start)] + ["0"] * 10
    with open(os.path.join(folder, "stat"), "w") as f:
        f.write(f"{pid} ({comm}) " + " ".join(fields) + "\n")


TD = ["/usr/bin/qemu-system-x86_64", "-accel", "kvm", "-name", "guest=web,debug-threads=on",
      "-machine", "q35,confidential-guest-support=tdx0",
      "-object", '{"qom-type":"tdx-guest","id":"tdx0"}', "-m", "8192M"]


class ProcTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        os.makedirs(os.path.join(self.root, "self"))   # not a pid
        self.host = Host(proc=self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_tdx_guests_are_found(self):
        fake_process(self.root, 100, TD)
        fake_process(self.root, 101, ["/usr/bin/qemu-system-x86_64", "-accel", "kvm", "-name", "plain"])
        fake_process(self.root, 102, ["/usr/bin/sudo", *TD], comm="sudo")
        fake_process(self.root, 103, ["/bin/sh", "-c", " ".join(TD)], comm="sh")
        fake_process(self.root, 104, TD, state="Z")
        os.makedirs(os.path.join(self.root, "105"))    # ended while it was being read
        self.assertEqual(self.host.guests(), [Guest(100, 4242, "web")])

    def test_a_name_with_spaces_in_the_stat_line_is_read(self):
        fake_process(self.root, 100, TD, comm="my guest (1)")
        self.assertEqual(self.host.guests(), [Guest(100, 4242, "web")])

    def test_names(self):
        name = guard_mod.guest_name
        self.assertEqual(name(["qemu", "-name", "web"], 7), "web")
        self.assertEqual(name(["qemu", "-name", "web,process=x"], 7), "web")
        self.assertEqual(name(["qemu", "-name", "process=x,guest=web"], 7), "web")
        self.assertEqual(name(["qemu", "-m", "1G"], 7), "pid7")

    def test_a_paused_guest_reads_as_paused(self):
        fake_process(self.root, 100, TD, state="T")
        self.assertTrue(self.host.is_paused(self.host.guests()[0]))

    def test_a_reused_pid_is_never_signalled(self):
        fake_process(self.root, 100, TD, start=4242)
        guest = Guest(100, 1111, "web")   # same pid, another start time
        sent = []
        real_kill = os.kill
        os.kill = lambda pid, sig: sent.append((pid, sig))
        try:
            with self.assertRaises(ProcessLookupError):
                self.host.pause(guest)
            self.host.resume(guest)
            self.host.pause(Guest(100, 4242, "web"))
        finally:
            os.kill = real_kill
        self.assertEqual(sent, [(100, signal.SIGSTOP)])


if __name__ == "__main__":
    unittest.main()
