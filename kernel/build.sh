#!/bin/bash
# Build Ubuntu 26.04's kernel with tdx-direct-map.patch applied.
#
#   ./build.sh [WORKDIR]
#
# Run it on Ubuntu 26.04, amd64. It downloads Ubuntu's linux source, applies
# the patch, and builds two packages that install beside the stock kernel.
# It installs nothing and never asks for root. A second run goes on from
# where the first one stopped. WORKDIR defaults to ./build and needs about
# 10 GB.
#
# Settings are environment variables:
#
#   SOURCE_VERSION  Ubuntu source version to build     default 7.0.0-43.43
#   ABI             number in the new kernel's name    default 41, which
#                   gives 7.0.0-41-generic
#   JOBS            parallel compile jobs              default: every CPU
set -euo pipefail

SOURCE_VERSION=${SOURCE_VERSION:-7.0.0-43.43}
ABI=${ABI:-41}
JOBS=${JOBS:-$(nproc)}

# The .dsc of the version the patch was written for. A .dsc lists the
# checksums of the other source files, so this one hash covers all of them.
PINNED_VERSION=7.0.0-43.43
PINNED_DSC_SHA256=05f8e99d36d8e532278720feef0cba5dce8d0d56162daf00f95daca7b4335628

say() { echo "==> $*"; }
die() { echo "build.sh: $*" >&2; exit 1; }

here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
patch_file=$here/tdx-direct-map.patch
work=${1:-$PWD/build}

[ -f "$patch_file" ] || die "tdx-direct-map.patch has to be next to this script"
[[ $SOURCE_VERSION =~ ^([0-9]+\.[0-9]+\.[0-9]+)-([0-9]+)\.([0-9]+)$ ]] ||
	die "SOURCE_VERSION has to look like 7.0.0-43.43"
upstream=${BASH_REMATCH[1]}
stock_abi=${BASH_REMATCH[2]}
upload=${BASH_REMATCH[3]}
[[ $ABI =~ ^[0-9]+$ ]] || die "ABI has to be a whole number"
[ "$ABI" != "$stock_abi" ] ||
	die "ABI $ABI is the number of Ubuntu's own kernel, and the build would replace that kernel"
[[ $JOBS =~ ^[1-9][0-9]*$ ]] || die "JOBS has to be a whole number above 0"

new_version=$upstream-$ABI.$upload
release=$upstream-$ABI-generic

# shellcheck disable=SC1091
. /etc/os-release
[ "${ID:-}" = ubuntu ] && [ "${VERSION_ID:-}" = 26.04 ] ||
	die "this builds on Ubuntu 26.04, and this machine runs ${PRETTY_NAME:-something else}"
[ "$(dpkg --print-architecture)" = amd64 ] || die "this builds on amd64"
for tool in curl dpkg-source dpkg-checkbuilddeps fakeroot patch python3 sha256sum; do
	command -v "$tool" >/dev/null ||
		die "$tool is missing. Install the tools with: sudo apt-get install -y curl dpkg-dev fakeroot patch python3"
done

mkdir -p "$work"
work=$(cd -- "$work" && pwd)
cd "$work"

# Launchpad keeps every published version. The archive mirrors keep only
# the newest one, so "apt-get source" stops finding an older version.
url=https://launchpad.net/ubuntu/+archive/primary/+sourcefiles/linux/$SOURCE_VERSION
fetch() {
	[ -f "$1" ] && return 0
	say "downloading $1"
	curl -fL --retry 3 --retry-delay 2 -o "$1.partial" "$url/$1"
	mv "$1.partial" "$1"
}

dsc=linux_$SOURCE_VERSION.dsc
fetch "$dsc"
if [ "$SOURCE_VERSION" = "$PINNED_VERSION" ]; then
	echo "$PINNED_DSC_SHA256  $dsc" | sha256sum -c --quiet - ||
		die "$dsc is not the file this script expects. Delete it and run again"
else
	say "note: $SOURCE_VERSION is not the version the patch was written for, and its .dsc is not checked against a known hash"
fi
sums=$(awk '/^Checksums-Sha256:/ {on = 1; next} /^[^ ]/ {on = 0} on {print $1 "  " $3}' "$dsc")
[ -n "$sums" ] || die "$dsc lists no checksums"
while read -r _ name; do
	fetch "$name"
done <<<"$sums"
echo "$sums" | sha256sum -c --quiet - ||
	die "a source file does not match $dsc. Delete it and run again"

tree=$work/linux-$upstream
if [ ! -d "$tree" ]; then
	say "unpacking the source"
	rm -rf "$tree.partial"
	dpkg-source -x "$dsc" "$tree.partial"
	mv "$tree.partial" "$tree"
fi
cd "$tree"
[ -f debian.master/changelog ] || die "$tree is not an Ubuntu kernel source tree"

if ! unmet=$(dpkg-checkbuilddeps -B 2>&1); then
	echo "$unmet" >&2
	die "build dependencies are missing. Install them and run this again:
    sudo apt-get build-dep -y --arch-only $work/$dsc"
fi

if grep -q tdx_hide_private_page arch/x86/virt/vmx/tdx/tdx.c; then
	say "the patch is already applied"
else
	say "applying the patch"
	patch -p1 --dry-run <"$patch_file" >/dev/null ||
		die "the patch does not apply to linux $SOURCE_VERSION. It was written for $PINNED_VERSION"
	patch -p1 <"$patch_file"
fi

# Ubuntu takes the number in the kernel's name from the first line of the
# changelog. A number of its own lets the new kernel install beside the
# stock one. The rules read debian.master/changelog and the packaging tools
# read debian/changelog.
first=$(head -n 1 debian.master/changelog)
case $first in
"linux ($new_version) "*) ;;
"linux ($SOURCE_VERSION) "*)
	sed -i "1s/(${SOURCE_VERSION//./\\.})/($new_version)/" debian.master/changelog
	;;
*) die "unexpected first changelog line: $first" ;;
esac
cp debian.master/changelog debian/changelog
chmod a+x debian/rules debian/scripts/* debian/scripts/misc/* || true

# Debug info is the long part of an Ubuntu kernel build, and nothing here
# needs it. Ubuntu keeps the configuration in an annotations file; this
# turns the debug-info symbols off for amd64 and changes nothing else.
python3 - debian.master/config/annotations <<'PY'
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
want = {
    "CONFIG_DEBUG_INFO": "n",
    "CONFIG_DEBUG_INFO_BTF": "n",
    "CONFIG_DEBUG_INFO_BTF_MODULES": "n",
    "CONFIG_DEBUG_INFO_COMPRESSED_NONE": "n",
    "CONFIG_DEBUG_INFO_DWARF5": "n",
    "CONFIG_GDB_SCRIPTS": "n",
    "CONFIG_DEBUG_INFO_NONE": "y",
}
seen = set()
out = []
for line in path.read_text().splitlines(keepends=True):
    name = line.split()[0] if line.split() else ""
    if name in want and "policy<" in line:
        new = want[name]
        old = "n" if new == "y" else "y"
        if f"'amd64': '{old}'" in line:
            line = line.replace(f"'amd64': '{old}'", f"'amd64': '{new}'", 1)
        elif f"'amd64': '{new}'" not in line:
            sys.exit("unexpected annotation line: " + line.strip())
        seen.add(name)
    out.append(line)
missing = sorted(set(want) - seen)
if missing:
    sys.exit("the annotations file has no line for: " + " ".join(missing))
path.write_text("".join(out))
PY

# Keep this machine's user and host name out of the kernel's version line.
export KBUILD_BUILD_USER=${KBUILD_BUILD_USER:-build}
export KBUILD_BUILD_HOST=${KBUILD_BUILD_HOST:-storm-research}

# One flavour, generic, which is the stock amd64 kernel. The switches skip
# what a test kernel does not need: perf and bpftool, the Rust library
# package, the debug-symbol and source packages, the ABI check that the
# renamed kernel would fail, the ZFS modules dependency, and the slowest
# module compression. The compile runs outside fakeroot, which would hold
# it to a few jobs; fakeroot is only for packing.
export CONCURRENCY_LEVEL=$JOBS
export DEB_BUILD_OPTIONS="parallel=$JOBS nocheck"
rules=(do_tools=false do_lib_rust=false do_skip_checks=true do_dbgsym_package=false
	do_linux_main_modules_depends=false do_source_package=false do_zstd_ko=)

# Ubuntu's own first step on a fresh tree. It restores what the source
# package format cannot carry, executable bits and deleted files, and it
# writes the package list for the new kernel name. It also deletes an
# earlier build, so it runs once for each tree.
if [ ! -f .storm-research-prepared ]; then
	say "preparing the tree"
	fakeroot debian/rules clean "${rules[@]}"
	touch .storm-research-prepared
fi

say "compiling $release with $JOBS jobs"
debian/rules "$PWD/debian/stamps/stamp-build-generic" "${rules[@]}"
say "packing"
debian/rules debian/control "${rules[@]}"
fakeroot debian/rules binary-generic "${rules[@]}"

image=$work/linux-image-unsigned-${release}_${new_version}_amd64.deb
modules=$work/linux-modules-${release}_${new_version}_amd64.deb
[ -f "$image" ] && [ -f "$modules" ] || die "the build ended without the two packages"

# grep -c reads to the end of its input, so the pipe never closes early.
in_modules() { dpkg-deb --fsys-tarfile "$modules" | tar -xO "./boot/$1-$release"; }
in_modules System.map | grep -c ' tdx_hide_ok$' >/dev/null ||
	die "the kernel that was built does not contain the patch"
for symbol in CONFIG_INTEL_TDX_HOST=y CONFIG_KVM_INTEL_TDX=y; do
	in_modules config | grep -c -x "$symbol" >/dev/null ||
		die "the kernel that was built has no $symbol"
done

echo
say "built $release. Install these two, the modules first:"
sha256sum "$modules" "$image"
