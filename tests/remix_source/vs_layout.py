"""Reads Splinter Cell Blacklist's DX9 vertex shaders (data/Shaders/d3d9/VertexShaders.bin).

The archive stores raw vs_3_0 bytecode. This module finds every shader, then reads its
constant table (CTAB), its input/output declarations and its instructions, so tests can
check the register layout that the Remix runtime changes depend on.
"""
import os
import struct
from dataclasses import dataclass, field

GAME_SHADERS = os.environ.get(
    "BLACKLIST_VS_BIN",
    r"D:\Steam\steamapps\common\Tom Clancy's Splinter Cell Blacklist\data\Shaders\d3d9\VertexShaders.bin",
)

VS30 = 0xFFFE0300
END = 0x0000FFFF
OP_DCL = 0x1F
OP_DEF = 0x51
OP_DEFB, OP_DEFI = 0x2F, 0x30
OP_COMMENT = 0xFFFE
USAGE = {0: "POSITION", 1: "BLENDWEIGHT", 2: "BLENDINDICES", 3: "NORMAL", 4: "PSIZE", 5: "TEXCOORD",
         6: "TANGENT", 7: "BINORMAL", 8: "TESSFACTOR", 9: "POSITIONT", 10: "COLOR", 11: "FOG", 12: "DEPTH", 13: "SAMPLE"}
REG_TEMP, REG_INPUT, REG_CONST, REG_OUTPUT = 0, 1, 2, 6
# call, callnz, loop, ret, endloop, label, rep, endrep, if, ifc, else, endif, break, breakc, breakp
NO_DST_OPS = {0x19, 0x1A, 0x1B, 0x1C, 0x1D, 0x1E, 0x26, 0x27, 0x28, 0x29, 0x2A, 0x2B, 0x2C, 0x2D, 0x60}


def reg_type(tok):
    return ((tok >> 28) & 7) | ((tok >> 8) & 0x18)


@dataclass
class Instr:
    opcode: int
    dst: tuple | None          # (regtype, regnum, writemask)
    srcs: list                 # [(regtype, regnum, swizzle, modifier)]


@dataclass
class Shader:
    offset: int
    words: list
    constants: dict = field(default_factory=dict)   # name -> (regset, index, count)
    inputs: dict = field(default_factory=dict)      # (usage, index) -> v#
    outputs: dict = field(default_factory=dict)     # (usage, index) -> o#
    instrs: list = field(default_factory=list)
    defs: dict = field(default_factory=dict)       # c# -> (x, y, z, w) literal constants

    def const(self, name):
        return self.constants.get(name)

    @property
    def is_world(self):
        return "gWorldToProj" in self.constants


def _parse_ctab(words, i, length, shader):
    raw = struct.pack("<%dI" % length, *words[i + 1:i + 1 + length])
    if raw[:4] != b"CTAB":
        return
    base = raw[4:]
    count, info = struct.unpack_from("<II", base, 12)
    for k in range(count):
        name_off, regset, regidx, regcount = struct.unpack_from("<IHHH", base, info + 20 * k)
        end = base.index(b"\0", name_off)
        shader.constants[base[name_off:end].decode("ascii", "replace")] = (regset, regidx, regcount)


def parse(words, offset=0):
    sh = Shader(offset, words)
    i = 1
    while i < len(words):
        tok = words[i]
        if tok == END:
            sh.words = words[:i + 1]
            break
        op = tok & 0xFFFF
        if op == OP_COMMENT:
            length = (tok >> 16) & 0x7FFF
            _parse_ctab(words, i, length, sh)
            i += 1 + length
            continue
        length = (tok >> 24) & 0x0F
        body = words[i + 1:i + 1 + length]
        if op == OP_DCL and len(body) == 2:
            usage, uidx = body[0] & 0x1F, (body[0] >> 16) & 0xF
            rt, rn = reg_type(body[1]), body[1] & 0x7FF
            if rt == REG_INPUT:
                sh.inputs[(USAGE.get(usage, usage), uidx)] = rn
            elif rt == REG_OUTPUT:
                sh.outputs[(USAGE.get(usage, usage), uidx)] = rn
        elif op == OP_DEF and len(body) == 5:
            sh.defs[body[0] & 0x7FF] = struct.unpack('<4f', struct.pack('<4I', *body[1:5]))
        elif op not in (OP_DCL, OP_DEF, OP_DEFB, OP_DEFI):
            # Control-flow tokens (if/loop/rep/...) have no destination; every source follows the opcode.
            has_dst = op not in NO_DST_OPS and len(body) > 0
            dst = (reg_type(body[0]), body[0] & 0x7FF, (body[0] >> 16) & 0xF) if has_dst else None
            srcs = []
            j = 1 if has_dst else 0
            while j < len(body):
                t = body[j]
                srcs.append((reg_type(t), t & 0x7FF, (t >> 16) & 0xFF, (t >> 24) & 0xF))
                j += 2 if (t >> 13) & 1 else 1   # relative addressing adds a token
            sh.instrs.append(Instr(op, dst, srcs))
        i += 1 + length
    return sh


def load(path=GAME_SHADERS):
    data = open(path, "rb").read()
    n = len(data) // 4
    words = struct.unpack("<%dI" % n, data[:n * 4])
    shaders = []
    w = 0
    while w < n:
        if words[w] == VS30:
            sh = parse(list(words[w:w + 65536]), w * 4)
            shaders.append(sh)
            w += len(sh.words)  # the version token also appears inside each constant table
        else:
            w += 1
    return shaders


if __name__ == "__main__":
    shaders = load()
    world = [s for s in shaders if s.is_world]
    print("vs_3_0 shaders:", len(shaders), "world (gWorldToProj):", len(world))
    names = {}
    for s in shaders:
        for nm, (rs, ri, rc) in s.constants.items():
            names.setdefault((nm, rs, ri, rc), 0)
            names[(nm, rs, ri, rc)] += 1
    for (nm, rs, ri, rc), c in sorted(names.items(), key=lambda x: (x[0][1], x[0][2])):
        print(f"  {nm:32s} set={rs} c{ri}..c{ri + rc - 1}  shaders={c}")
