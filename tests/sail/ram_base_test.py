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
        final.command('x-sail-ram-base-load', filename=str(base), id=base_id)
        final.load(third)
        assert final.read(16 << 20, len(payload)) == changed
        assert final.read(0x70000, 4) == destination.read(0x70000, 4)
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
        print(json.dumps({'accel': args.accel, 'machine': args.machine, 'full_bytes': full.stat().st_size,
                          'delta_bytes': delta.stat().st_size, 'followup_bytes': third.stat().st_size,
                          'first_dirty_bytes': first_dirty, 'correctness': 'passed'}))


if __name__ == '__main__':
    main()
