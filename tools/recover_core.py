#!/usr/bin/env python3
"""Recover Hoiko's unmanaged CIL algorithms as portable C++.

Only application algorithms and their reachable dependencies are emitted.
The original instruction stream, branch targets and data tables are preserved.
Windows UI, C++ CRT file I/O and worker-thread setup are replaced at explicit
boundaries; no original Windows assembly is loaded at runtime.
"""
import argparse
import collections
import hashlib
import json
from pathlib import Path

import dnfile
from dncil.cil.body.reader import read_method_body_from_bytes


class Reader:
    def __init__(self, data):
        self.data, self.pos = data, 0

    def byte(self):
        b = self.data[self.pos]
        self.pos += 1
        return b

    def uint(self):
        b = self.byte()
        if b < 128:
            return b
        if b < 192:
            return ((b & 63) << 8) | self.byte()
        return ((b & 31) << 24) | (self.byte() << 16) | (self.byte() << 8) | self.byte()


class Recovery:
    def __init__(self, source):
        self.source = source
        self.pe = dnfile.dnPE(str(source))
        self.tables = self.pe.net.mdtables
        self.sizes = {s.Parent.row_index: s.ClassSize for s in self.tables.ClassLayout}
        self.methods = {0x06000000 + i: m for i, m in enumerate(self.tables.MethodDef, 1)}
        self.fields = {0x04000000 + i: f for i, f in enumerate(self.tables.Field, 1)}
        self.rvas = {0x04000000 + f.Field.row_index: f.Rva for f in self.tables.FieldRva}
        self.bodies = {}
        self.selected = set()
        self.used_fields = set()
        self.used_overrides = {}
        self.externals = {}

    def typ(self, reader):
        e = reader.byte()
        if e in (0x1f, 0x20):
            reader.uint()
            return self.typ(reader)
        if e in (0x45, 0x41):
            return self.typ(reader)
        primitive = {1: (0, 'void'), 2: (1, 'u8'), 3: (2, 'u16'), 4: (1, 'i8'),
                     5: (1, 'u8'), 6: (2, 'i16'), 7: (2, 'u16'), 8: (4, 'i32'),
                     9: (4, 'i32'), 10: (8, 'i64'), 11: (8, 'i64'),
                     12: (4, 'f32'), 13: (8, 'f64'), 14: (8, 'i64'),
                     0x18: (8, 'i64'), 0x19: (8, 'i64'), 0x1c: (8, 'i64')}
        if e in primitive:
            return primitive[e]
        if e in (0x0f, 0x10, 0x1d):
            self.typ(reader)
            return (8, 'i64')
        if e in (0x11, 0x12):
            token = reader.uint()
            size = self.sizes.get(token >> 2, 4) if token & 3 == 0 else 8
            if e == 0x12:
                size = 8
            return (size, 'blob' if size not in (1, 2, 4, 8) else
                    {1: 'u8', 2: 'u16', 4: 'i32', 8: 'i64'}[size])
        if e == 0x1b:
            self.signature(reader)
            return (8, 'i64')
        raise ValueError(f'Unsupported signature element {e:x}')

    def signature(self, reader):
        flags = reader.byte()
        if flags & 0x10:
            reader.uint()
        count = reader.uint()
        ret = self.typ(reader)
        args = [self.typ(reader) for _ in range(count)]
        if flags & 0x20:
            args.insert(0, (8, 'i64'))
        return ret, args

    def row(self, token):
        table, index = token >> 24, (token & 0xffffff) - 1
        if table == 6:
            return self.methods[token]
        if table == 10:
            return self.tables.MemberRef.rows[index]
        if table == 0x2b:
            ref = self.tables.MethodSpec.rows[index].Method
            return ref.row
        raise ValueError(f'Not a method: {token:x}')

    def name(self, token):
        return str(self.row(token).Name)

    def body(self, token):
        if token not in self.bodies:
            m = self.methods[token]
            if not m.Rva or not m.ImplFlags.miIL:
                raise ValueError(f'Native method needs replacement: {token:x} {m.Name}')
            self.bodies[token] = read_method_body_from_bytes(self.pe.get_data(m.Rva, 0x20000))
        return self.bodies[token]

    def override(self, token):
        name = self.name(token)
        if name in ('new', 'new[]'):
            return 'allocate'
        if name in ('delete', 'delete[]'):
            return 'deallocate'
        if name == '_Xtime_get_ticks':
            return 'ticks'
        if name == 'Exchange' and token >> 24 == 10:
            return 'exchange'
        if name.startswith('std._Atomic_storage') and name.endswith('.load'):
            return 'atomic_load'
        if name == 'SearchAI.MultiEvaluator.ThreadStart':
            return 'sequential_worker'
        if name == 'EvaluateAI.ReadEvalWeight':
            return 'weights_ready'
        if name.startswith('std.partial_sort<class Node'):
            return 'partial_sort'
        if name.startswith('std.partition<class Node'):
            return 'partition_node'
        if name.startswith('std.partition<class ExpandAI'):
            return 'partition_baby'
        if name == 'SearchAI.Init':
            return 'engine_init'
        return None

    def select(self, roots):
        queue = list(roots)
        while queue:
            token = queue.pop()
            if token in self.selected or token in self.used_overrides:
                continue
            replacement = self.override(token)
            if replacement:
                self.used_overrides[token] = replacement
                continue
            self.selected.add(token)
            for i in self.body(token).instructions:
                if i.opcode.name in ('call', 'ldftn'):
                    queue.append(i.operand.value)
                if i.opcode.name in ('ldsfld', 'ldsflda', 'stsfld'):
                    self.used_fields.add(i.operand.value)

    def field_type(self, token):
        r = Reader(self.fields[token].Signature.value)
        assert r.byte() == 6
        return self.typ(r)

    @staticmethod
    def id(token):
        return f'm_{token:08x}'

    @staticmethod
    def fid(token):
        return f'g_{token:08x}'

    def emit(self, dest):
        roots = [token for token, m in self.methods.items()
                 if str(m.Name) in ('SearchAI.Search', 'SearchAI.ReadConfig', 'SearchAI.Init',
                                    'RootNode.Init', 'RootNode.SetNewCurrent',
                                    'RootNode.ResetPPTData', 'RootNode.SetPPTData',
                                    'RootNode.IsSameAs', 'Board.UpdateMaxHeight',
                                    'Board.LockMino', 'Board.ClearLine', 'FallMino.Spawn',
                                    'HoldMino.PPT1or2', 'NextMino.ViewNext',
                                    'EvaluateAI.EvenOrOdd')]
        self.select(roots)
        # Configuration and weights are initialized by the portable bridge.
        for token, f in self.fields.items():
            if any(key in str(f.Name) for key in ('@ConfigAI@@', '@HoldMino@@',
                                                '@NextMino@@', '@EvaluateAI@@', '@SearchAI@@')):
                self.used_fields.add(token)
        dest.mkdir(parents=True, exist_ok=True)
        out = ['// Generated from the supplied Hoiko.exe. See tools/recover_core.py.',
               '#include "cil_runtime.hpp"', 'namespace recovered {']
        manifest = {'sha256': hashlib.sha256(self.source.read_bytes()).hexdigest(),
                    'methods': {}, 'fields': {}, 'overrides': {},
                    'safety_guards': {'Board.LockMino': 'Reject out-of-bounds or overlapping four-cell landings before original memory writes'}}
        for token in sorted(self.used_fields):
            size, kind = self.field_type(token)
            raw = self.pe.get_data(self.rvas.get(token, 0), size) if token in self.rvas else b''
            raw = raw.ljust(size, b'\0')[:size]
            out.append(f'// {self.fields[token].Name}')
            out.append(f'alignas(8) unsigned char {self.fid(token)}[{size}] = {{' +
                       ','.join(str(b) for b in raw) + '};')
            manifest['fields'][str(self.fields[token].Name)] = {'id': self.fid(token),
                                                               'size': size, 'kind': kind}
        for token in sorted(self.selected | self.used_overrides.keys()):
            out.append(f'Slot {self.id(token)}(const Slot*);')
            manifest['methods'].setdefault(self.name(token), []).append(
                {'id': self.id(token), 'token': hex(token),
                 'signature': self.signature(Reader(self.row(token).Signature.value))})
        for token, replacement in sorted(self.used_overrides.items()):
            manifest['overrides'][self.name(token)] = replacement
            out.append(f'Slot {self.id(token)}(const Slot* a) {{ return native_{replacement}(a); }}')
        for token in sorted(self.selected):
            out.extend(self.emit_method(token))
        out.append('} // namespace recovered')
        (dest / 'core.inc').write_text('\n'.join(out) + '\n')
        (dest / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        bindings = ['// Generated symbols for the supplied binary.']
        for name, entries in manifest['methods'].items():
            if '<' in name or '{' in name:
                continue
            for entry in entries:
                alias = name.replace('.', '_') + '_' + str(len(entry['signature'][1]))
                bindings.append(f'#define R_{alias} recovered::{entry["id"]}')
        for name, entry in manifest['fields'].items():
            import re
            match = re.fullmatch(r'\?(\w+)@(\w+)@@.*', name)
            if match:
                bindings.append(f'#define G_{match[2]}_{match[1]} recovered::{entry["id"]}')
        (dest / 'bindings.inc').write_text('\n'.join(bindings)+'\n')
        print(f'Recovered {len(self.selected)} CIL methods; {len(self.used_overrides)} platform replacements; '
              f'{len(self.used_fields)} data fields')

    def emit_method(self, token):
        body = self.body(token)
        ret, args = self.signature(Reader(self.row(token).Signature.value))
        locals_ = []
        if body.local_var_sig_tok:
            r = Reader(self.tables.StandAloneSig.rows[(body.local_var_sig_tok.value & 0xffffff) - 1].Signature.value)
            assert r.byte() == 7
            locals_ = [self.typ(r) for _ in range(r.uint())]
        out = [f'// {self.name(token)}', f'Slot {self.id(token)}(const Slot* input) {{',
               f'Slot a[{max(1, len(args))}]{{}};',
               f'for(int i=0;i<{len(args)};++i) a[i]=input[i];',
               f'Slot s[{max(16,body.max_stack + 2)}]{{}}; int sp=0;']
        if self.name(token) == 'Board.LockMino':
            out.insert(2, 'if(!valid_lock(input[0].ptr(),input[1].ptr())) return Slot::i32(-1);')
        for j, (size, _) in enumerate(locals_):
            out.append(f'alignas(8) unsigned char l{j}[{max(size,8)}]{{}};')

        def push(expr):
            return f's[sp++]={expr};'

        for inst in body.instructions:
            op, v = inst.opcode.name, inst.operand
            out.append(f'L{inst.offset}:; // {op}')
            if op in ('nop', 'unaligned.', 'volatile.', 'tail.'):
                continue
            if op == 'ret':
                out.append('return {};' if ret[0] == 0 else 'return s[--sp];')
            elif op.startswith('ldc.i4'):
                n = int(op.rsplit('.', 1)[1]) if op.rsplit('.', 1)[1].isdigit() else (-1 if op.endswith('m1') else v)
                out.append(push(f'Slot::i32({n})'))
            elif op == 'ldc.i8':
                out.append(push(f'Slot::i64(UINT64_C({v & ((1<<64)-1)}))'))
            elif op in ('ldc.r8', 'ldc.r4'):
                out.append(push(f'Slot::real({v!r})'))
            elif op.startswith(('ldarg', 'starg')):
                j = int(op[-1]) if op[-1].isdigit() else v.index
                if op.startswith('ldarga'):
                    out.append(push(f'Slot::pointer(&a[{j}].u)'))
                elif op.startswith('ldarg'):
                    out.append(push(f'a[{j}]'))
                else:
                    out.append(f'a[{j}]=s[--sp];')
            elif op.startswith(('ldloc', 'stloc')):
                j = int(op[-1]) if op[-1].isdigit() else v.index
                size, kind = locals_[j]
                if op.startswith('ldloca'):
                    out.append(push(f'Slot::pointer(l{j})'))
                elif op.startswith('ldloc'):
                    out.append(push(f'load_{kind}(l{j})'))
                else:
                    out.append(f'store(l{j},s[--sp],{size});')
            elif op == 'ldsflda':
                out.append(push(f'Slot::pointer({self.fid(v.value)})'))
            elif op == 'ldsfld':
                out.append(push(f'load_{self.field_type(v.value)[1]}({self.fid(v.value)})'))
            elif op == 'stsfld':
                out.append(f'store({self.fid(v.value)},s[--sp],{self.field_type(v.value)[0]});')
            elif op.startswith('ldind.'):
                kind = {'i':'i64','ref':'i64','r4':'f32','r8':'f64','i1':'i8','u1':'u8',
                        'i2':'i16','u2':'u16','i4':'i32','u4':'i32','i8':'i64'}[op[6:]]
                out.append(f's[sp-1]=load_{kind}(s[sp-1].ptr());')
            elif op.startswith('stind.'):
                size = {'i':8,'ref':8,'r4':4,'r8':8,'i1':1,'i2':2,'i4':4,'i8':8}[op[6:]]
                out.append(f'store(s[sp-2].ptr(),s[sp-1],{size}); sp-=2;')
            elif op == 'dup':
                out.append('s[sp]=s[sp-1]; ++sp;')
            elif op == 'pop':
                out.append('--sp;')
            elif op in ('add','sub','mul','div','div.un','rem','rem.un','and','or','xor','shl','shr','shr.un'):
                out.append(f's[sp-2]=binary_{op.replace(".","_")}(s[sp-2],s[sp-1]); --sp;')
            elif op in ('not', 'neg'):
                out.append(f's[sp-1]=unary_{op}(s[sp-1]);')
            elif op.startswith('conv.'):
                out.append(f's[sp-1]=convert_{op[5:].replace(".","_")}(s[sp-1]);')
            elif op in ('ceq','cgt','cgt.un','clt','clt.un'):
                comparison = {'ceq':'eq','cgt':'gt','clt':'lt'}[op.split('.')[0]]
                out.append(f's[sp-2]=Slot::i32(compare(s[sp-2],s[sp-1],Cmp::{comparison},'
                           f'{str(".un" in op).lower()})); --sp;')
            elif op in ('br', 'br.s', 'leave', 'leave.s'):
                out.append(('sp=0; ' if op.startswith('leave') else '') + f'goto L{v};')
            elif op.startswith(('brtrue','brfalse')):
                out.append(f'if({"!" if op.startswith("brfalse") else ""}s[--sp].truth()) goto L{v};')
            elif op.startswith(('beq','bne','bge','bgt','ble','blt')):
                comparison = {'beq':'eq','bne':'ne','bge':'ge','bgt':'gt','ble':'le','blt':'lt'}[op[:3]]
                out.append(f'sp-=2; if(compare(s[sp],s[sp+1],Cmp::{comparison},'
                           f'{str(".un" in op).lower()})) goto L{v};')
            elif op == 'switch':
                out.append('switch(s[--sp].i()) {' + ''.join(f'case {j}: goto L{label};' for j, label in enumerate(v)) + '}')
            elif op == 'call':
                callret, callargs = self.signature(Reader(self.row(v.value).Signature.value))
                out.append(f'{{ sp-={len(callargs)}; Slot result={self.id(v.value)}(s+sp);'
                           + ('s[sp++]=result;' if callret[0] else '') + '}')
            elif op == 'ldftn':
                out.append(push(f'Slot::pointer((void*)&{self.id(v.value)})'))
            elif op == 'ldobj':
                # All reachable ldobj instructions pass a one-byte predicate by value.
                type_index = v.value & 0xffffff
                size = self.sizes.get(type_index, 4)
                if size > 8:
                    raise ValueError(f'Unsupported ldobj size {size}')
                kind = {1:'u8',2:'u16',4:'i32',8:'i64'}[size]
                out.append(f's[sp-1]=load_{kind}(s[sp-1].ptr());')
            elif op == 'cpblk':
                out.append('std::memmove(s[sp-3].ptr(),s[sp-2].ptr(),s[sp-1].u); sp-=3;')
            elif op == 'initblk':
                out.append('std::memset(s[sp-3].ptr(),s[sp-2].u,s[sp-1].u); sp-=3;')
            elif op == 'localloc':
                out.append('s[sp-1]=Slot::pointer(__builtin_alloca(s[sp-1].u));')
            elif op in ('endfinally','endfilter','rethrow','throw'):
                out.append('throw std::runtime_error("Unexpected original C++ exception path");')
            else:
                raise ValueError(f'Unsupported opcode {op} in {self.name(token)}')
        out.append('throw std::runtime_error("CIL method fell through");\n}')
        return out


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('binary', type=Path)
    parser.add_argument('--output', type=Path, default=Path('native/generated'))
    args = parser.parse_args()
    Recovery(args.binary).emit(args.output)
