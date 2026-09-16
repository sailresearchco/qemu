This is a Sail-specific experiment based on QEMU v11.0.4. It is not an upstream
QEMU API or an upstream contribution. The changes and tests were generated with
AI assistance; upstream QEMU's code-provenance policy does not accept them.

The x-sail-ram-base-* QMP commands retain a conservative RAM dirty bitmap across
migration jobs. Creating a base requires paused CPUs; loading requires a fresh
incoming VM. Normal streams remain normal unless a base is explicitly selected.
Selected streams carry a base identity and are deliberately rejected by stock
QEMU. Without explicit advancement, failure/cancellation retains dirty history.
`x-sail-ram-base-advance` arms an incremental local export at the final frozen
RAM boundary. QEMU writes only explicit changed chunks (including zero chunks)
and metadata. Selected zero chunks remain explicit in the replacement bitmap,
but their output bytes use sparse holes rather than disk writes. The fixture
checks both their zero content and lack of added payload allocation. A second
base record announces the successor before the final
RAM round. Source and receiver retain writes after that boundary for the next
capture. Shared storage merges replacements into a flattened manifest; QEMU
never fetches or publishes storage. An unpublished successor cannot match the
owner's committed checkpoint identity and therefore cannot be reused.

Incremental advance and destination export files are temporary local artifacts:
QEMU completes their writes and closes them, but does not `fsync` this disposable
copy. The owner authenticates their bytes and establishes recovery availability
and durable publication using its own objects before releasing the old owner.
Export success alone has never established a durable Sail checkpoint. The
initial standalone full-base capture retains its existing file sync behavior.

A live receiver starts from the old base. A durable restore can start from the
flattened successor and specify the predecessor as `stream-id`; it still replays
the complete native stream and must observe the matching advance record. No
intermediate checkpoint manifests or device-state chain are needed. The owner
may export selected paused destination chunks for recovery caching, but must
verify their hashes: final-round writes and device-load fixups can differ from
the captured base. Such mismatches require the authoritative source bytes.

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

New captures use SAILRAM2, with each RAM block aligned to 4096 bytes. On Linux,
loading privately maps eligible fixed anonymous blocks at their existing host
addresses, so the kernel faults in base pages only when needed. Incoming delta
writes are copy-on-write. Legacy SAILRAM1 bases and ineligible allocations keep
the eager reader. No source connection or object-store access lives in QEMU.
The caller must authenticate and retain immutable local bytes: it may unlink
the private base path after loading, but must not rewrite or truncate its inode
while a VM maps it. The mapping retains the inode until process exit.

Mapped RAM retains anonymous discard semantics: discard replaces the range
with anonymous zero pages, never MADV_DONTNEED (which would reveal old file
bytes). Discard still invalidates the epoch. The qualification checks mapping
residency before guest access, old-format restore, truncated-file rejection,
copy-on-write isolation, repeated discard, and reads after the path is unlinked.
Its explicit qtest socket has a Sail-only discard hook for this boundary; the
production QMP API does not expose that test operation. This is local lazy
loading, not a lazy S3 source: Sail still prepares the complete local base.
