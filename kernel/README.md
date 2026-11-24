# TDX direct-map patch

A patch for the host kernel of a machine that runs Intel TDX guests.

While a guest owns a page of memory, the host kernel has no mapping of that page. A stray host write into guest memory then faults in the host, with a call trace, and does not land in the guest.

It is an experiment against one fault on one kind of machine. It has run on one host since 2026-11-24. The [results so far](../README.md#results-so-far) are on the front page.

## Target

| | |
|---|---|
| CPU | Intel Xeon 6746E (Sierra Forest), one socket, 112 cores |
| Host OS | Ubuntu 26.04 LTS, amd64 |
| Kernel source | Ubuntu `linux` 7.0.0-43.43 |
| TDX module | 1.5.36 |
| Guests | QEMU 10.2, private memory from guest_memfd |

Nothing in the patch depends on the CPU model. It changes code that every TDX host on this kernel runs. The table is the one place it has run.

On a different kernel version the patch has to be carried over by hand. `build.sh` stops when it does not apply.

## Why

In the mode TDX runs in on this host, every 64-byte line of memory carries one extra bit that says a TDX guest wrote it. A write that uses a host key clears the bit. The guest's next read of that line is treated as poisoned memory and raises a machine check. On the target host that machine check becomes the [SMI storm](../guard/README.md#the-problem).

The host kernel keeps all RAM mapped writable in one large mapping of its own, called the direct map. A guest's private pages are in it like any other page. Any kernel code with a stale pointer or a late copy can therefore write into guest memory. Nothing stops the write and nothing logs it.

What writes into guest memory on the target host is not known. The direct map is the one path the kernel can close by itself, so the patch closes it. The outcome says whether that path was the one. See [What a result means](#what-a-result-means).

## What the patch does

1. **Split.** guest_memfd is the kernel code that holds a guest's private memory. Before it hands a page to KVM, it splits the direct map around that page down to 4 KiB entries. A split can allocate memory and sleep, and the next step runs under a lock where that is not allowed, so it happens here.
2. **Hide.** The TDX module gives a page to a guest with one of two calls: `TDH.MEM.PAGE.ADD` while the guest is built, `TDH.MEM.PAGE.AUG` when a running guest touches new memory. After either call returns success, the kernel flushes the page's cache lines once more, clears the page's entry in the direct map, and flushes the kernel TLB on every CPU. The cache flush comes first because it faults on a page that is not mapped. If the module refused the page, nothing is changed.
3. **Leave the rest.** The module's own page tables for the guest (Secure EPT), the guest's control pages and the source page of an ADD stay mapped.
4. **Restore.** When a page is taken back from a guest, the entry is put back after the module has flushed and released the page (`TDH.PHYMEM.PAGE.WBINVD`), and before the kernel resets the page's contents, which it does through the direct map.
5. **Count.** `/proc/tdx_direct_map` shows four counters: `hide_ok`, `hide_fail`, `unshield_ok`, `unshield_fail`.

| File | Change |
|---|---|
| `arch/x86/virt/vmx/tdx/tdx.c` | The hide and restore functions, the counters and the proc file. Hide is called after ADD and AUG |
| `arch/x86/kvm/vmx/tdx.c` | Restore, in `tdx_sept_remove_private_spte` |
| `virt/kvm/guest_memfd.c` | The 4 KiB split, in `kvm_gmem_get_pfn` and `__kvm_gmem_populate` |
| `arch/x86/include/asm/tdx.h` | One declaration |
| `arch/x86/mm/pat/set_memory.c`, `arch/x86/mm/tlb.c` | Four existing functions exported, because KVM is a module |

116 added lines. The whole patch is [`tdx-direct-map.patch`](tdx-direct-map.patch).

## What it costs

- **Guest start and stop are slower.** Every page given to a guest, and every page taken back, costs a TLB flush on every CPU. The cost was not measured.
- **The direct map stays split.** Memory that a guest has used stays mapped in 4 KiB entries until the host reboots. `DirectMap4k` in `/proc/meminfo` grows by that amount.
- **A caught write is a host crash.** A kernel write to a hidden page is a page fault in kernel mode, which is a kernel oops. With `kernel.panic_on_oops=1` the host panics and every guest on it is lost. That trace is what the patch is for. A crash-dump service (kdump) keeps it.

## Get the packages

### From the release

The [Releases](https://github.com/Secret3dev/storm-research/releases) page has the two packages built by `build.sh`, a `SHA256SUMS` file, and the Ubuntu source they were built from.

```
sha256sum -c SHA256SUMS
```

### Build them

On Ubuntu 26.04, amd64. It does not have to be the TDX host.

```
sudo apt-get install -y curl dpkg-dev fakeroot patch python3
./build.sh
```

The first run stops if build dependencies are missing and prints the one `apt-get build-dep` command that installs them. After that, the same `./build.sh` goes on from where it stopped.

What the script does:

1. Downloads Ubuntu's `linux` 7.0.0-43.43 source from Launchpad and checks it against a hash kept in the script.
2. Applies the patch, after a dry run.
3. Gives the kernel its own number, so that it installs beside Ubuntu's kernel and replaces nothing. See `ABI` below.
4. Turns debug info off, which is the long part of an Ubuntu kernel build. Everything else in the configuration is Ubuntu's generic amd64 kernel.
5. Runs Ubuntu's own preparation step on the fresh tree, once.
6. Compiles and packs.
7. Checks that the packed kernel contains the patch and has TDX host support on.

It never asks for root and installs nothing. The work directory is `./build` unless another path is given as the first argument. It takes about 10 GB.

| Variable | Default | Meaning |
|---|---|---|
| `SOURCE_VERSION` | `7.0.0-43.43` | Ubuntu source version. The patch was written for the default |
| `ABI` | `41` | The number in the new kernel's name: `7.0.0-41-generic`. It has to be a number that no installed Ubuntu kernel uses. A number below Ubuntu's own keeps Ubuntu's kernel first in the boot menu |
| `JOBS` | every CPU | Parallel compile jobs |
| `KBUILD_BUILD_USER`, `KBUILD_BUILD_HOST` | `build`, `storm-research` | The user and host shown in `/proc/version`. Set by default so that the build machine's own names stay out of the kernel |

The path of the work directory is compiled into the kernel as well. A build that will be shared wants a path with nothing private in it.

The result is two files in the work directory:

```
linux-modules-7.0.0-41-generic_7.0.0-41.43_amd64.deb
linux-image-unsigned-7.0.0-41-generic_7.0.0-41.43_amd64.deb
```

## Install

No kernel named `7.0.0-41` may be installed already. This prints nothing on a host that is ready:

```
dpkg -l | grep 7.0.0-41
```

Then the modules first:

```
sudo dpkg -i linux-modules-7.0.0-41-generic_7.0.0-41.43_amd64.deb
sudo dpkg -i linux-image-unsigned-7.0.0-41-generic_7.0.0-41.43_amd64.deb
ls /boot/vmlinuz-7.0.0-41-generic /boot/initrd.img-7.0.0-41-generic
```

Installing does not reboot and removes nothing. Ubuntu's kernel stays installed and stays the default.

- **Secure Boot.** The kernel is not signed. With Secure Boot on, it has to be signed with a key the firmware trusts, or it does not boot.
- **No headers package.** Out-of-tree modules (DKMS) cannot be built for this kernel.
- **No BTF.** Debug info is off, so tools that read `/sys/kernel/btf/vmlinux` do not work on this kernel.

## Boot it

`7.0.0-41` sorts below Ubuntu's kernels, so the boot menu still starts Ubuntu's kernel by default. The patched kernel is the entry `Ubuntu, with Linux 7.0.0-41-generic` under `Advanced options for Ubuntu`.

To start it on the next boot only:

```
sudo grub-reboot "Advanced options for Ubuntu>Ubuntu, with Linux 7.0.0-41-generic"
```

Three things to know first:

- **Any later reboot goes back to Ubuntu's kernel**, a crash included. The protection is then off until the patched kernel is chosen again.
- **GRUB may not be able to forget the choice.** It clears a one-time choice by writing to `/boot/grub/grubenv`, and it cannot write there when `/boot` is on software RAID or LVM. The choice then holds for every boot until it is cleared from Linux: `sudo grub-editenv /boot/grub/grubenv unset next_entry`. The `grub-reboot` line above is the standard Ubuntu way. It is not how the test host was started, so that line itself has not been exercised.
- **A host with a paused or frozen guest must not be rebooted by software.** See [Why it never kills](../guard/README.md#why-it-never-kills).

## Check that it works

```
uname -r                    # 7.0.0-41-generic
cat /proc/tdx_direct_map
grep DirectMap4k /proc/meminfo
```

| Counter | Counts | Healthy |
|---|---|---|
| `hide_ok` | Pages taken out of the direct map | Rises when a guest starts or touches new memory |
| `hide_fail` | Pages a guest got that could not be hidden. They stay mapped, as on a stock kernel | 0 |
| `unshield_ok` | Pages put back | Rises when a guest stops |
| `unshield_fail` | Pages that could not be put back | 0 |

The counters start at 0 on every boot. A missing `/proc/tdx_direct_map` means the running kernel is not the patched one.

## Go back

Reboot into Ubuntu's kernel, then:

```
sudo apt-get remove --purge linux-image-unsigned-7.0.0-41-generic linux-modules-7.0.0-41-generic
```

## What a result means

| Seen on the patched kernel | Meaning |
|---|---|
| Storms go on as before | The damage does not come through the kernel's direct map. What is left: a device writing by DMA, the firmware, the TDX module, or the CPU |
| The host oopses on a page fault at a direct-map address, with a driver or a copy routine in the trace | The writer is caught. The trace names it |
| No storm and no oops, for much longer than the storms used to stay away | The direct map was the path, but nothing names the code that used it. Two ways that can happen: a copy to or from user space is refused without a log line, and a CPU side effect through the mapping leaves no trace at all |

## Known gaps

Two faults in the patch as it stands. Neither has been seen on the test host: both fail counters are 0.

1. **A page that could not be put back is freed while hidden.** If the TDX module refuses to release a page, or the restore fails, the page stays out of the direct map and guest_memfd frees it anyway. The next kernel code that gets that page faults at a place that has nothing to do with the cause. The fix is to keep a reference on such a page so that it is never reused. It is not in this version.
2. **A page's second use is not fully covered.** The restore puts the direct-map entry back without its "global" flag. On a CPU that tags TLB entries by process (PCID) with page-table isolation off, which is the case on the target CPU, the one-page flush at the next hide does not reach entries cached under another process's tag. A write through such a stale entry can still land. Pages on their first use by a guest since boot are not affected.

Smaller points:

- Every private page fault takes a global lock for the 4 KiB split, even when the split was done long ago.
- The TLB flush on restore is not needed.

## What it does not cover

- **Writes that do not go through the direct map:** a device writing by DMA, the firmware in system-management mode, the TDX module, and the CPU itself.
- **The storm.** A line that gets damaged some other way still becomes a storm, exactly as before. The [guard](../guard/) is still needed.
- **Guests that are already damaged.** The patch protects pages from the moment a guest gets them on a patched boot.

## License

The patch is a change to the Linux kernel and carries the kernel's license, GPL-2.0-only. `build.sh` is MIT, like the rest of this repository.
