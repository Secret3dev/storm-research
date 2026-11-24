<p align="center">
  <img src="assets/storm-research.png" alt="storm research" width="440">
</p>

# storm research

We bought a Xeon 6746E to run confidential VMs. 112 cores, TDX, the works.

Turns out one VM reading one bad 64-byte line of memory makes the whole box eat 2,000 SMIs a second, on every core, until something stops it. And killing that VM to make it stop resets the server. We know because it did that to us four times.

We went looking for the fix. Nothing from Intel, nothing in the kernel, and the internet has apparently never heard of this. So we wrote our own: a guard that freezes the VM before it drags the host down with it, and a kernel patch that should stop the memory getting trashed to begin with.

No guarantees it works. Same promise Intel made for TDX on this CPU. But your paperweight might get a second life.

## What is here

| | What it does | State |
|---|---|---|
| [`guard/`](guard/) | A small service. It notices the storm, finds the guest that drives it and pauses that one guest. The host and the other guests keep running | In use. It has ended every storm it has met, each by pausing one guest |
| [`kernel/`](kernel/) | A host kernel patch, a build script, and built packages on the [Releases](https://github.com/Secret3dev/storm-research/releases) page. While a guest owns a page of memory, the host kernel cannot write to it | Experimental. First boot on 2026-11-24. See [Results so far](#results-so-far) |

The guard contains the fault. The patch is an attempt at its cause. They are meant to run together.

## The fault

Intel TDX runs a virtual machine whose memory the host cannot read. Such a guest is called a TD here.

1. **A line of guest memory gets damaged.** Something on the host writes into a TD's private memory. The write does not use the TD's key, so the 64-byte line it touches is marked poisoned. What does the writing is not known. The damage is always in the first line of a 4 KiB page, and it arrives when the host or the guests are busy with disk and network work. → the [kernel patch](kernel/)
2. **The guest reads the line and the host stalls.** The read should end in a machine check that stops one guest. On this CPU and firmware it becomes a system-management interrupt (SMI) instead, the guest is sent straight back to retry the read, and the loop runs about 2,000 times a second. Every round stops every CPU. → the [guard](guard/)
3. **The guest cannot be cleaned up.** Stopping or killing a guest that is in the loop has reset the whole machine. It stays paused until the host is power-cycled.

**What the CPU is missing.** Intel's condition for turning machine checks into SMIs on a TDX host is a CPU capability called SEAM_NR, bit 12 of the register `IA32_MCG_CAP`. The Xeon 6746E does not report it: `rdmsr -p 0 0x179` prints `f000c20`, and bit 12 (`0x1000`) is clear. The firmware turns the SMI mode on anyway. So a machine check inside a guest arrives as a plain SMI with no failure flag, the TDX module reports success, and the kernel sends the guest back in to read the same line again.

The whole chain in step 2 is written out in the [guard's README](guard/README.md#the-problem).

## Is a host affected?

Everything here was found and run on one kind of machine:

| | |
|---|---|
| CPU | Intel Xeon 6746E (Sierra Forest), one socket, 112 cores |
| Host OS | Ubuntu 26.04 LTS, kernel 7.0 |
| TDX module | 1.5.36 |
| Guests | QEMU 10.2 |

The signs on a running host, and the three conditions for the storm, are in the [guard's README](guard/README.md#is-a-host-exposed). The short test is the SMI counter, read twice one second apart:

```
rdmsr -p 0 -d 0x34; sleep 1; rdmsr -p 0 -d 0x34
```

A difference under 20 is calm. A storm is in the thousands.

## Quick start

### The guard

```
cd guard
install -m 0755 smi-storm-guard.py /usr/local/sbin/smi-storm-guard
install -m 0644 smi-storm-guard.service /etc/systemd/system/smi-storm-guard.service
modprobe msr
smi-storm-guard --list
```

`--list` shows the guests it would watch and changes nothing. The rest, including a watch-only mode and a rehearsal, is in [`guard/README.md`](guard/README.md).

### The patched kernel

For Ubuntu 26.04 on amd64. Either download the two packages from the [Releases](https://github.com/Secret3dev/storm-research/releases) page, or build them:

```
cd kernel
./build.sh
```

Then install them beside Ubuntu's kernel:

```
sudo dpkg -i linux-modules-7.0.0-41-generic_7.0.0-41.43_amd64.deb
sudo dpkg -i linux-image-unsigned-7.0.0-41-generic_7.0.0-41.43_amd64.deb
```

Nothing is replaced, and the host keeps starting Ubuntu's kernel until the patched one is chosen at boot. How to boot it, check it and remove it, what it costs, and its known gaps are in [`kernel/README.md`](kernel/README.md).

## Results so far

One host, one boot of the patched kernel, read on 2026-11-24 at 19:18 UTC.

| | |
|---|---|
| Time on the patched kernel | 12 h 44 min |
| TDX guests started / stopped | 15 / 7 |
| Pages hidden from the host kernel | 12.7 million, 0 failures |
| Pages put back | 7.4 million, 0 failures |
| Storms | 0 |
| Host errors | 0 |

This does not show yet that the patch stops the storms. On the six boots before it, the first storm came between 4 minutes and 22.5 hours after boot, and most of those boots carried more guest starts and stops than this one has so far.

What it does show: every part of the patch that normal operation exercises works, on every page, without a failure.

## What is still open

- What writes into guest memory.
- Whether the patch stops the storms. [What a result means](kernel/README.md#what-a-result-means) lists the three ways this can end.
- Two [known gaps](kernel/README.md#known-gaps) in the patch.
- The storm itself. A damaged line still becomes a storm. Nothing here changes the firmware setting or the kernel loop that turn one bad read into a stalled host.

## License

MIT, see [LICENSE](LICENSE). The kernel patch is a change to Linux and carries the kernel's license, GPL-2.0-only.
