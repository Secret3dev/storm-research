# SMI storm guard

A small guard for Linux hosts that run Intel TDX guests.

On some hosts, one bad memory read inside a TDX guest turns into a flood of system-management interrupts (SMIs) that stalls the whole machine. This guard notices the flood, finds the guest that drives it, and pauses that one guest. The host and the other guests keep running.

It contains the problem. It does not repair the cause. The [kernel patch](../kernel/) is the attempt at the cause.

## The problem

What the host looks like during a storm:

- One TDX guest stops answering.
- The host and every other guest become very slow. The kernel log fills with soft lockups and RCU stalls.
- The kernel log has no machine-check line.
- The SMI counter, MSR `0x34`, climbs by about 1,500 to 2,100 a second on every CPU. On a healthy host it does not move.

What happens underneath, as far as it was established on the host where this was written:

1. A vCPU inside a TDX guest reads a memory line that is marked poisoned. The normal result is a machine check.
2. The firmware is set to turn uncorrected machine checks into SMIs. This mode is called eMCA generation 2, and setup screens label it with names such as "MCE-SMI enable" or "MSMI". Intel names a CPU capability as the precondition for that mode: bit 12 of `IA32_MCG_CAP` (SEAM_NR). On this CPU the bit is 0 and the firmware turns the mode on anyway.
3. The SMI ends the guest's run with the exit reason "other SMI". The TDX module marks a guest as failed only when that exit carries its machine-check flag. Here it arrives without the flag, so the module reports success.
4. KVM has nothing to do for "other SMI" and enters the guest again (`tdx_handle_exit` in Linux 7.0). The guest retries the same read. Nothing limits the rate.
5. The loop runs about 2,000 times a second. Every round is an SMI, and an SMI stops every CPU while the firmware handles it.

The poisoned line can come from a real memory fault or from a write to the guest's private memory that did not use the guest's key. The guard does not care which.

This was seen on a Xeon 6 E-core host (Sierra Forest, Xeon 6746E) with TDX module 1.5.36, Linux 7.0 and QEMU 10.2.

## Is a host exposed?

All three have to be true:

- It runs TDX guests.
- `IA32_MCG_CAP` bit 12 is 0. `rdmsr -p 0 0x179` prints the register. The affected host reads `f000c20`, where bit 12 (`0x1000`) is clear.
- The firmware turns machine checks into SMIs. Some boards do not show this setting in setup.

To see whether a storm is running now, read the SMI count twice, one second apart:

```
rdmsr -p 0 -d 0x34; sleep 1; rdmsr -p 0 -d 0x34
```

A difference under 20 is calm. A storm is in the thousands.

## What the guard does

1. It reads the SMI count on one CPU once a second.
2. A storm is 200 SMIs a second or more for 3 seconds in a row.
3. It pauses the running TDX guests one at a time, by sending `SIGSTOP` to the QEMU process. After each pause it waits 0.3 seconds and measures for 1 second. If the rate fell under 20 a second, that guest is the one in the loop. If not, the guest is resumed and the next one is tried.
4. If no single pause ends the storm, it pauses all of them together. If that ends the storm, it resumes them one at a time and pauses again each guest whose resume brings the storm back. If the storm goes on with every guest paused, no TDX guest is the cause, and all are resumed.
5. The guest in the loop stays paused. The guard calls that guest parked, and keeps the record under `/run`, so it lasts until the next boot.
6. Each step is written to the journal and to a log file that is synced to disk line by line. For a parked guest it also saves the thread list and the last five minutes of the kernel log.

A healthy guest is paused for about 1.3 seconds during a search. A guest that something else had already paused is skipped and left as it was.

The guard parks at most 3 guests in an hour. After that it only watches and logs, until it is restarted.

## Why pausing works

KVM enters a guest only when the guest's vCPU thread asks it to. A stopped thread never asks, so the poisoned read is not retried and no further SMI is raised. On the affected host the rate fell to zero within a second of the pause, every time.

`SIGCONT` undoes it. The storm was back within a second when a parked guest was resumed.

## Why it never kills

The guard has no code that stops, kills or restarts a guest.

On the affected host, ending a guest that was in this loop reset the whole machine most of the times it was tried. The BMC logged a processor CATERR. An earlier version of this guard killed the guest it had found, and the host reset in that second. The one time the host survived, the poisoned memory went back to the host. A later allocation of that memory hung, and the network card dropped off the bus until a power cycle.

The likely reason is that tearing a TDX guest down makes the TDX module walk and flush all of the guest's memory, the poisoned line included.

So a parked guest is left exactly as it is until the host can be power-cycled:

- No `SIGCONT`.
- No stop, restart or kill, and no service manager whose stop timeout ends in a kill.
- No watchdog or health check that restarts a guest which stopped answering.
- No software shutdown or reboot of the host, because that ends the guest on the way down. On the affected host everything else is stopped first, and the machine is then power-cycled from the BMC.

The guest's memory and whatever it was doing are lost at that power cycle.

## Install

It needs Linux, root, Python 3, the `msr` kernel module, and guests that run as QEMU processes. From this directory:

```
install -m 0755 smi-storm-guard.py /usr/local/sbin/smi-storm-guard
install -m 0644 smi-storm-guard.service /etc/systemd/system/smi-storm-guard.service
modprobe msr
smi-storm-guard --list
```

`--list` prints the pid, state and name of every guest the guard would watch, and the SMI rate. It changes nothing. Every TDX guest on the host has to be in that list, and nothing else. If the list is wrong, set the two patterns under Settings.

To start it watch-only:

```
mkdir -p /etc/smi-storm-guard
touch /etc/smi-storm-guard/off
systemctl enable --now smi-storm-guard
```

While that file exists the guard logs a storm and pauses nothing. Removing the file lets it act from the next storm on. No restart is needed.

## Rehearsal

```
smi-storm-guard --drill PID
```

This runs the real search with a made-up SMI count that climbs for as long as the guest with that pid is running. The pauses are real. Guests with a lower pid are each paused for about 1.3 seconds, the chosen guest is paused for a few seconds, and then everything is resumed. It prints `DRILL: passed` when the guard found and parked the right guest. Nothing stays parked afterwards.

## Settings

Settings are environment variables. In the unit file they are `Environment=` lines.

| Variable | Default | Meaning |
|---|---|---|
| `GUARD_BINARY` | `^qemu-system-x86` | Regular expression matched against the file name of a process's program |
| `GUARD_ARGS` | `tdx-guest` | Regular expression matched against the rest of its command line. A process is a TDX guest when both match |
| `GUARD_STORM_RATE` | `200` | SMIs a second that count as a storm |
| `GUARD_CALM_RATE` | `20` | Under this the storm is over |
| `GUARD_STORM_SECONDS` | `3` | Seconds the rate has to stay high before any guest is paused |
| `GUARD_MAX_PARKS` | `3` | Guests parked in one hour before the guard only watches |
| `GUARD_CPU` | `0` | The CPU whose SMI count is read |
| `GUARD_OFF_FILE` | `/etc/smi-storm-guard/off` | While this file exists the guard only watches |
| `GUARD_STATE_DIR` | `/run/smi-storm-guard` | `parked.json` and `status.json`. Keep it on a file system that is emptied at boot |
| `GUARD_LOG_DIR` | `/var/log/smi-storm-guard` | `guard.log` and one file per parked guest |

The default patterns match QEMU started with a `tdx-guest` object, which is how QEMU and libvirt start a TDX guest. A guest gets its name from QEMU's `-name` option, and its pid when there is none.

## What it does not do

- It does not remove the cause. The memory line is still poisoned, and the firmware still turns the machine check into an SMI. The [kernel patch](../kernel/) tries to keep the line from being poisoned in the first place.
- It does not cover the other way this fault can end. The machine check can also arrive while the TDX module itself is running. The module then shuts down, and every later call into it fails with `0x8000ff00ffff0000`. All TDX guests are dead at once and there is nothing to pause. The host needs a reboot.
- It reads one CPU's counter. That is enough when the SMI reaches every CPU, as it did here.
- It has run only against QEMU on Linux 7.0 on the CPU named above. Other virtual machine monitors need their own patterns and have not been tried.

## Tests

```
python3 test_guard.py
```

The tests run the guard against a model of a host. They touch no machine and need no root.

## License

MIT. See [LICENSE](../LICENSE).
