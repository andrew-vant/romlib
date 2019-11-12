# At the end of the monster structure are up to three optional fields
# pointing to their loot table, combat script, and resistances. Each set is a
# one-byte integer type indicator followed by a two-byte pointer. The type
# indicator has three known values: 0x03 (drop pointer), 0x07 (script pointer),
# and 0x08 (resist pointer). 0x00 means stop. The fields, if present, always
# appear in this order.
#
# Monsters with no drop pointer never drop anything; monsters with no script
# pointer only attack; monsters with no resist pointer have neutral resists.
#
# I am not sure what to do if a different type indicator is encountered. One
# possibility is to throw an exception, but that precludes experimenting with
# other values. A second possibility is to log a warning and treat it as if it
# were a stop marker. A third is to continue onward and store the pointer as an
# unknown -- I am not sure if or whether that will break other things.
#
# When logging such warnings it should probably record the faulty type code and
# the location the pointer would have gone if it were valid, so one can
# manually investigate if desired.
#
# FIXME: Is there a way to alter a logger within the main dump/load loop such
# that it can print the index of the monster being processed even if the
# function logging the message does not know it?

import logging
from pprint import pprint
from bitstring import BitArray

log = logging.getLogger(__name__)

codes = {0x03: "drop",
         0x07: "script",
         0x08: "resist"}

def make_struct(base):
    class monster(base):
        def read_extra(self, bs):
            nextcode = lambda: bs.read(8).uint
            for code in iter(nextcode, 0):
                if code not in codes:
                    # Log a warning rather than raising an exception; otherwise
                    # you won't be able to dump bugged monsters.
                    msg = "Unknown monster pointer code: %s, targeting %s"
                    log.warning(msg, code, pointer)
                    break
                field = self.fields[codes[code]]
                if code in [0x07, 0x08]:
                    self.data[field.id] = field(self, bs)
                elif code == 0x03:
                    drop, chance = bs.read(16).bytes
                    # The low bit of chance is actually the high bit of drop,
                    # so mod as needed:
                    if chance % 2:
                        drop += 256
                    chance = chance >> 1
                    self.drop = drop
                    self.dchance = chance

        def extra_bytes(self, offset):
            offset += self.base_size
            bytemap = {}
            for code, fid in sorted(codes.items()):
                if fid not in self:
                    continue
                bytemap[offset] = code
                offset += 1
                bits = BitArray()

                if fid == "drop":
                    bs_chance = self.data['dchance'].bits
                    bs_drop = self.data['drop'].bits
                    bits.append(bs_drop[1:])
                    bits.append(bs_chance)
                    bits.append(bs_drop[:1])
                else:
                    bits = self.data[fid].bits

                for byte in bits.bytes:
                    bytemap[offset] = byte
                    offset += 1
            return bytemap
    return monster
