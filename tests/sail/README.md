This is a Sail-specific experiment based on QEMU v11.0.4. It is not an upstream
QEMU API or an upstream contribution. The changes and tests were generated with
AI assistance; upstream QEMU's code-provenance policy does not accept them.

The x-sail-ram-base-* QMP commands retain a conservative RAM dirty bitmap across
migration jobs. Creating a base requires paused CPUs; loading requires a fresh
incoming VM. Normal streams remain normal unless a base is explicitly selected.
Selected streams carry a base identity and are deliberately rejected by stock
QEMU. Source failure/cancellation does not consume the epoch. The receiver keeps
all pages changed relative to the base, so another migration can use that base.

The caller authenticates the immutable base's bytes and manages storage,
publication, disk consistency and which worker may run the VM. IDs are unique
epoch identities, not checksums calculated by QEMU. A partial incoming failure
requires discarding that VM. This version invalidates the base after RAM discard
or topology/size changes and requires full migration. It does not qualify GPU
passthrough, postcopy, mapped-RAM, shared/private-memory exceptions or hotplug.

Run `python3 tests/sail/ram_base_test.py /absolute/qemu-system-x86_64` and repeat
with `--accel kvm --machine q35` on an x86 KVM host. The fixture executes a small
counter guest, changes and zeroes RAM, repeats captures, cancels/retries a
migration, migrates again from the receiver, rejects wrong bases and starts a
new epoch. It compares all populated RAM bytes. It is not a Sailbox benchmark.

Use `--memory-mib 16384` on a dedicated host to exercise a larger RAM geometry.
The retry runs guest CPUs during migration, and the receiver must preserve a
small accumulated dirty set across another move. This fixture does not touch
all guest RAM after restore: KVM can conservatively dirty newly mapped writable
pages on their first access, including reads. A full guest workload benchmark
must measure that case separately; the persistent bitmap does not eliminate it.
