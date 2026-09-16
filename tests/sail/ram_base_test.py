#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Sail fork integration tests: native RAM bases, retries and repeated moves.

Runs actual CPU writes (TCG by default; --accel kvm on an x86 KVM host),
compares every byte of a populated region, and rejects mismatched bases.
This is a correctness fixture, not the Sailbox performance benchmark.
"""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import random
import shutil
import socket
import subprocess
import tempfile
import time


class VM:
    def __init__(self, binary, root, name, bios, accel, incoming=False, machine='microvm', memory_mib=128):
        self.root = root
        self.name = name
        self.log = (root / (name + '.log')).open('wb')
        qmp = root / (name + '.qmp')
        qtest = root / (name + '.qtest')
        args = [binary, '-machine', machine, '-accel', accel, '-cpu', 'max' if accel == 'tcg' else 'host',
                '-m', str(memory_mib)+'M', '-smp', '1', '-nodefaults', '-display', 'none',
                '-monitor', 'none', '-serial', 'none', '-bios', str(bios), '-S',
                '-qmp', f'unix:{qmp},server=on,wait=off',
                '-qtest', f'unix:{qtest},server=on,wait=off', '-qtest-log', '/dev/null']
        if incoming:
            args += ['-incoming', 'defer']
        self.process = subprocess.Popen(args, stdout=self.log, stderr=self.log)
        self.sockets = []
        try:
            self.qmp = self.connect(qmp)
            assert 'QMP' in json.loads(self.qmp.readline())
            self.command('qmp_capabilities')
            self.qtest = self.connect(qtest)
        except BaseException:
            self.close()
            raise

    def connect(self, path):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            s = socket.socket(socket.AF_UNIX)
            s.settimeout(30)
            try:
                s.connect(str(path))
                self.sockets.append(s)
                return s.makefile('rwb', buffering=1 << 20)
            except OSError:
                s.close()
                if self.process.poll() is not None:
                    raise RuntimeError((self.root / (self.name + '.log')).read_text())
                time.sleep(.01)
        raise TimeoutError(path)

    def command(self, name, **args):
        request = {'execute': name, 'arguments': args}
        self.qmp.write(json.dumps(request).encode() + b'\n')
        self.qmp.flush()
        while True:
            data = self.qmp.readline()
            if not data:
                raise RuntimeError(f'{self.name} QMP closed: ' + (self.root / (self.name + '.log')).read_text())
            response = json.loads(data)
            if 'event' in response:
                continue
            if 'error' in response:
                raise RuntimeError(response['error'])
            return response['return']

    def test(self, command):
        self.qtest.write(command.encode() + b'\n')
        self.qtest.flush()
        result = self.qtest.readline().decode().strip()
        assert result.startswith('OK'), result
        return result[2:].strip()

    def write(self, addr, data):
        self.test(f'write {addr:#x} {len(data):#x} 0x{data.hex()}')

    def read(self, addr, size):
        return bytes.fromhex(self.test(f'read {addr:#x} {size:#x}')[2:])

    def status(self):
        return self.command('query-status')

    def wait_migration(self, expected='completed'):
        until = time.monotonic() + 30
        while time.monotonic() < until:
            info = self.command('query-migrate')
            state = info.get('status')
            if state == expected:
                return info
            if state in ('failed', 'cancelled'):
                raise RuntimeError(info)
            time.sleep(.01)
        raise TimeoutError(info)

    def base_info(self):
        # Native cleanup is scheduled after query-migrate first says completed.
        until = time.monotonic() + 5
        while True:
            try:
                return self.command('query-sail-ram-base')
            except RuntimeError as e:
                if 'cleanup' not in str(e) or time.monotonic() > until:
                    raise
                time.sleep(.01)

    def save(self, path):
        self.command('migrate', uri='file:' + str(path))
        result = self.wait_migration()
        self.base_info()
        return result

    def load(self, path):
        self.command('migrate-incoming', uri='file:' + str(path))
        self.wait_migration()
        assert not self.status()['running']

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        for s in self.sockets:
            s.close()
        self.log.close()


def rejects(fn, contains):
    try:
        fn()
    except RuntimeError as e:
        assert contains in str(e), str(e)
    else:
        raise AssertionError('operation unexpectedly accepted')


def legacy_base(source, target):
    """Re-encode the same epoch with the old unaligned file layout."""
    with source.open('rb') as src, target.open('wb') as dst:
        header = src.read(72)
        assert header[:8] == b'SAILRAM2'
        dst.write(b'SAILRAM1' + header[8:])
        while first := src.read(1):
            header = first + src.read(first[0] + 8)
            length = int.from_bytes(header[-8:], 'big')
            dst.write(header)
            src.seek((src.tell() + 4095) & ~4095)
            while length:
                data = src.read(min(length, 1 << 20))
                assert data
                if data == bytes(len(data)):
                    dst.seek(len(data), 1)
                else:
                    dst.write(data)
                length -= len(data)
        dst.truncate()


def mapped_base_memory(vm, filename):
    """Only the base mapping, excluding QEMU code/device/bitmap allocations."""
    selected = False
    values = {'Size': 0, 'Rss': 0}
    for line in Path(f'/proc/{vm.process.pid}/smaps').read_text().splitlines():
        if line and line[0] in '0123456789abcdef' and '-' in line.split()[0]:
            selected = str(filename) in line
        elif selected and ':' in line:
            key, value = line.split(':', 1)
            if key in values:
                values[key] += int(value.split()[0]) * 1024
    return values


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('binary')
    parser.add_argument('--accel', default='tcg', choices=['tcg', 'kvm'])
    parser.add_argument('--machine', default='microvm', choices=['microvm', 'q35'])
    parser.add_argument('--memory-mib', type=int, default=128)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='sail-ram-') as tmp, contextlib.ExitStack() as stack:
        root = Path(tmp)
        rom = bytearray(65536)
        # Increment a RAM counter continuously without firmware or an OS.
        rom[:13] = bytes([0xfa, 0xb8, 0, 0x70, 0x8e, 0xd8, 0x66, 0xff, 6, 0, 0, 0xeb, 0xf9])
        rom[65520:65525] = bytes([0xea, 0, 0, 0, 0xf0])
        bios = root / 'counter.rom'
        bios.write_bytes(rom)
        def vm(name, incoming=False):
            value = VM(args.binary, root, name, bios, args.accel, incoming, args.machine, args.memory_mib)
            stack.callback(value.close)
            return value
        source = vm('source')
        payload = random.Random(42).randbytes(16 << 20)
        for offset in range(0, len(payload), 1 << 20):
            source.write((16 << 20) + offset, payload[offset:offset + (1 << 20)])
        source.command('cont')
        time.sleep(.1)
        source.command('stop')
        assert int.from_bytes(source.read(0x70000, 4), 'little') > 0
        full = root / 'full.stream'
        source.save(full)
        restored = vm('full', True)
        restored.load(full)
        assert restored.read(16 << 20, len(payload)) == payload
        base = root / 'base.ram'
        base_id = hashlib.sha256(b'immutable-base-1').hexdigest()
        info = source.command('x-sail-ram-base-create', filename=str(base), id=base_id)
        assert info['valid'] and info['dirty-bytes'] == 0, info
        assert base.stat().st_size >= 128 << 20
        with base.open('rb') as f:
            base_hash = hashlib.file_digest(f, 'sha256').hexdigest()
        # Recovery exports must retain explicit zero bytes without allocating
        # their payload. Both exports include the same nonzero metadata chunks.
        export_chunk = 1 << 20
        with base.open('rb') as f:
            zero_index = None
            for index in range(1, base.stat().st_size // export_chunk):
                f.seek(index * export_chunk)
                if f.read(export_chunk) == bytes(export_chunk):
                    zero_index = index
                    break
        assert zero_index is not None
        metadata_export = root / 'metadata.export'
        zero_export = root / 'zero.export'
        for output, indices in [(metadata_export, []), (zero_export, [zero_index])]:
            source.command('x-sail-ram-base-export', filename=str(output),
                           id=base_id, **{'chunk-size': export_chunk, 'chunks': indices})
        with zero_export.open('rb') as f:
            f.seek(zero_index * export_chunk)
            assert f.read(export_chunk) == bytes(export_chunk)
        assert zero_export.stat().st_size == metadata_export.stat().st_size
        assert zero_export.stat().st_blocks <= metadata_export.stat().st_blocks + 8
        # Validate lazy mapping before any guest reads could fault base pages.
        lazy = vm('lazy', True)
        started = time.monotonic()
        lazy.command('x-sail-ram-base-load', filename=str(base), id=base_id)
        lazy_seconds = time.monotonic() - started
        memory = mapped_base_memory(lazy, base)
        assert memory['Size'] >= args.memory_mib * (1 << 20), memory
        assert memory['Rss'] < 4 << 20, memory
        assert lazy.read(16 << 20, len(payload)) == payload
        ram_name = 'microvm.ram' if args.machine == 'microvm' else 'pc.ram'
        lazy.write(16 << 20, b'\x55' * 4096)
        lazy.test(f'sail-ram-discard {ram_name} {16 << 20:#x} 4096')
        assert lazy.read(16 << 20, 4096) == bytes(4096)
        assert not lazy.base_info()['valid']
        # Discarding twice must not resurrect the file's original bytes.
        lazy.write(16 << 20, b'\x77' * 4096)
        lazy.test(f'sail-ram-discard {ram_name} {16 << 20:#x} 4096')
        assert lazy.read(16 << 20, 4096) == bytes(4096)
        lazy.close()
        # Old durable bases continue to restore through the eager reader.
        legacy = root / 'legacy.ram'
        legacy_base(base, legacy)
        old = vm('legacy', True)
        old.command('x-sail-ram-base-load', filename=str(legacy), id=base_id)
        assert old.read(16 << 20, len(payload)) == payload
        old.close()
        truncated = root / 'truncated.ram'
        shutil.copyfile(base, truncated)
        with truncated.open('r+b') as f:
            f.truncate(truncated.stat().st_size - 1)
        invalid = vm('truncated', True)
        rejects(lambda: invalid.command('x-sail-ram-base-load', filename=str(truncated), id=base_id), 'size')
        assert mapped_base_memory(invalid, truncated)['Size'] == 0
        invalid.close()
        # A deferred receiver may be seeded, but not changed after listening.
        pending = vm('pending', True)
        pending.command('x-sail-ram-base-load', filename=str(base), id=base_id)
        pending.command('migrate-incoming', uri='unix:' + str(root / 'pending.sock'))
        rejects(lambda: pending.command('query-sail-ram-base'), 'cleanup')
        rejects(lambda: pending.command('x-sail-ram-base-select', id=base_id, enabled=True), 'cleanup')
        rejects(lambda: pending.command('x-sail-ram-base-create', filename=str(root / 'pending.ram'), id='c' * 64), 'cleanup')
        pending.close()
        before = source.read(0x70000, 4)
        source.command('cont')
        time.sleep(.1)
        rejects(lambda: source.command('x-sail-ram-base-create', filename=str(root / 'running'), id='b' * 64), 'paused')
        source.command('stop')
        assert source.read(0x70000, 4) != before
        # Non-CPU writes and newly zeroed pages must also survive.
        changed = bytearray(payload)
        changed[8192:16384] = b'\x00' * 8192
        changed[24576:28672] = b'\xa5' * 4096
        source.write((16 << 20) + 8192, changed[8192:16384])
        source.write((16 << 20) + 24576, changed[24576:28672])
        info = source.base_info()
        assert 16384 <= info['dirty-bytes'] < 1 << 20, info
        rejects(lambda: source.command('x-sail-ram-base-select', id='0' * 64, enabled=True), 'mismatched')
        source.command('x-sail-ram-base-select', id=base_id, enabled=True)
        delta = root / 'delta.stream'
        source.save(delta)
        assert delta.stat().st_size < full.stat().st_size // 8, (delta.stat(), full.stat())
        first_dirty = source.base_info()['dirty-bytes']
        assert first_dirty >= 16384
        # Repeating capture must not clear accumulated history.
        second = root / 'delta2.stream'
        source.command('cont')
        time.sleep(.02)
        source.command('stop')
        source.save(second)
        assert source.base_info()['dirty-bytes'] == first_dirty
        # Cancelling a partial transfer must preserve the epoch for a retry.
        source.command('cont')
        time.sleep(.02)
        source.command('stop')
        source.command('migrate-set-parameters', **{'max-bandwidth': 1024})
        source.command('migrate', uri='file:' + str(root / 'cancelled.stream'))
        time.sleep(.1)
        source.command('migrate_cancel')
        source.wait_migration('cancelled')
        assert source.base_info()['dirty-bytes'] == first_dirty
        source.command('migrate-set-parameters', **{'max-bandwidth': 1 << 30})
        retry = root / 'retry.stream'
        source.command('cont')
        source.save(retry)
        destination = vm('delta', True)
        destination.command('x-sail-ram-base-load', filename=str(base), id=base_id)
        destination.load(retry)
        assert destination.read(16 << 20, len(payload)) == changed
        assert destination.read(0x70000, 4) == source.read(0x70000, 4)
        received_dirty = destination.base_info()['dirty-bytes']
        assert first_dirty <= received_dirty < 1 << 20, received_dirty
        # Move again from the receiver; earlier changed pages remain required.
        destination.command('cont')
        time.sleep(.1)
        destination.command('stop')
        changed[32768:36864] = b'\x7c' * 4096
        destination.write((16 << 20) + 32768, changed[32768:36864])
        destination.command('x-sail-ram-base-select', id=base_id, enabled=True)
        third = root / 'delta3.stream'
        destination.save(third)
        assert third.stat().st_size < full.stat().st_size // 8, third.stat().st_size
        final = vm('final', True)
        unlinked = root / 'unlinked.ram'
        shutil.copyfile(base, unlinked)
        final.command('x-sail-ram-base-load', filename=str(unlinked), id=base_id)
        unlinked.unlink()
        final.load(third)
        assert final.read(16 << 20, len(payload)) == changed
        assert final.read(0x70000, 4) == destination.read(0x70000, 4)
        # Advancing a base replaces only changed chunks, and the flattened
        # successor can restore without fetching its parent's manifest.
        advance_id = hashlib.sha256(b'advanced-epoch').hexdigest()
        advance_file = root / 'advance.chunks'
        chunk_size = 1 << 20
        destination.command('x-sail-ram-base-advance', filename=str(advance_file),
                            **{'parent-id': base_id, 'id': advance_id,
                               'chunk-size': chunk_size})
        advanced_stream = root / 'advanced.stream'
        destination.command('cont')
        time.sleep(.02)
        destination.command('stop')
        destination.save(advanced_stream)
        advanced = destination.base_info()
        assert advanced['id'] == advance_id, advanced
        assert advanced['advanced-from'] == base_id, advanced
        assert advanced['dirty-bytes'] < first_dirty, advanced
        assert len(advanced['chunks']) < advanced['file-size'] // chunk_size // 4
        advanced_base = root / 'advanced.ram'
        shutil.copyfile(base, advanced_base)
        with advanced_base.open('r+b') as output, advance_file.open('rb') as patch:
            for index in advanced['chunks']:
                offset = index * chunk_size
                patch.seek(offset)
                output.seek(offset)
                data = patch.read(min(chunk_size, advanced['file-size'] - offset))
                output.write(data)
        # Both old-base streaming and new-base durable restore reach the same
        # RAM state and new dirty epoch.
        for name, seed, seed_id, stream_id in [
            ('advanced-live', base, base_id, None),
            ('advanced-durable', advanced_base, advance_id, base_id),
        ]:
            receiver = vm(name, True)
            kwargs = {'stream-id': stream_id} if stream_id else {}
            receiver.command('x-sail-ram-base-load', filename=str(seed),
                             id=seed_id, **kwargs)
            receiver.load(advanced_stream)
            assert receiver.base_info()['id'] == advance_id
            assert receiver.read(16 << 20, len(payload)) == changed
            assert receiver.read(0x70000, 4) == destination.read(0x70000, 4)
        missing_marker = vm('missing-advance-record', True)
        missing_marker.command('x-sail-ram-base-load',
                               filename=str(advanced_base), id=advance_id,
                               **{'stream-id': base_id})
        try:
            missing_marker.load(third)
        except (OSError, RuntimeError):
            assert missing_marker.process.wait(timeout=5) != 0
        else:
            raise AssertionError('flattened base accepted no advance record')
        # A later capture never includes the already committed old dirty set.
        destination.command('cont')
        time.sleep(.02)
        destination.command('stop')
        destination.write((16 << 20) + 65536, b'\x91' * 4096)
        destination.command('x-sail-ram-base-select', id=advance_id, enabled=True)
        after_advance = root / 'after-advance.stream'
        destination.save(after_advance)
        after_info = destination.base_info()
        assert 4096 <= after_info['dirty-bytes'] < first_dirty, after_info
        after_receiver = vm('after-advance', True)
        after_receiver.command('x-sail-ram-base-load',
                               filename=str(advanced_base), id=advance_id)
        after_receiver.load(after_advance)
        expected_advanced = bytearray(changed)
        expected_advanced[65536:69632] = b'\x91' * 4096
        assert after_receiver.read(16 << 20, len(payload)) == expected_advanced
        # Refusing the output before any reset preserves the current epoch;
        # no unpublished successor may be reported as usable.
        destination.command('cont')
        time.sleep(.02)
        destination.command('stop')
        destination.command('x-sail-ram-base-advance', filename='/missing/sail/base',
                            **{'parent-id': advance_id, 'id': 'd' * 64,
                               'chunk-size': chunk_size})
        try:
            destination.save(root / 'failed-advance.stream')
        except RuntimeError:
            pass
        else:
            raise AssertionError('failed export reported success')
        assert destination.base_info()['id'] == advance_id

        # Unseeded receiver must fail closed instead of filling unchanged RAM
        # with zeros and reporting a completed migration.
        wrong = vm('unseeded', True)
        try:
            wrong.load(delta)
        except (OSError, RuntimeError):
            assert wrong.process.wait(timeout=5) != 0
            assert 'RAM base identity mismatch' in (root / 'unseeded.log').read_text()
        else:
            raise AssertionError('unseeded delta activated')
        wrong_file = vm('wrong-file', True)
        rejects(lambda: wrong_file.command('x-sail-ram-base-load', filename=str(base), id='e' * 64), 'identity')
        # An explicit fresh base resets the epoch, including TCG TLB state.
        next_base = root / 'next.ram'
        next_id = hashlib.sha256(b'immutable-base-2').hexdigest()
        final.command('x-sail-ram-base-create', filename=str(next_base), id=next_id)
        assert final.base_info()['dirty-bytes'] == 0
        final.command('cont')
        time.sleep(.02)
        final.command('stop')
        assert final.base_info()['dirty-bytes'] >= 4096
        with base.open('rb') as f:
            assert hashlib.file_digest(f, 'sha256').hexdigest() == base_hash
        print(json.dumps({'accel': args.accel, 'machine': args.machine, 'full_bytes': full.stat().st_size,
                          'delta_bytes': delta.stat().st_size, 'followup_bytes': third.stat().st_size,
                          'first_dirty_bytes': first_dirty, 'lazy_load_seconds': lazy_seconds,
                          'base_mapping': memory, 'correctness': 'passed'}))


if __name__ == '__main__':
    main()
