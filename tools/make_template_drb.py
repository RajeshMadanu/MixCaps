"""Build assets/mixcaps-template.drb: a Resolve bin holding one Text+ title ("MixCaps Caption").

MixCaps imports this with MediaPool:ImportFolderFromFile(), then places that title once per caption, the
same way AutoSubs does - so nobody ever has to drag Text+ into the Media Pool.

.drb format (worked out from AutoSubs' caption-bin.drb; AutoSubs is MIT licensed, (c) Tom Moroney,
https://github.com/tmoroney/auto-subs - its bin scaffolding XML is reused here with fresh IDs):
  zip: project.xml, MediaPool/Master/MpFolder.xml, MediaPool/Master/<NNN_Folder>/MpFolder.xml
  Sm2MpGenerator  = a Fusion Title media pool item
    FieldsBlob    = u32 BE 2, u32 BE len, 0x80, protobuf {1:{2:name, 6:4}, 2:"Fusion Title", 3:{1:double secs},
                    5:varint frames, 6:"Text+", 7:"Text+"}
    CompositionBA = u32 BE raw_len + zlib(Qt QVariantMap {count, 0_name, 0_mediaIdList, 0_data})
      0_data      = "Composition { ... Compressed = true, }" + 0x00 + u32 LE tools_len + zlib(tools text)

Usage: python make_template_drb.py <autosubs caption-bin.drb> <out .drb>
"""
import io
import re
import struct
import sys
import uuid
import zipfile
import zlib

TEMPLATE_NAME = "MixCaps Caption"
FOLDER_NAME = "MixCaps Template"

COMP_HEADER = (
    'Composition { CurrentTime = 0, RenderRange = { 0, 0 }, GlobalRange = { 0, 1000 }, CurrentID = 5, HiQ = true, '
    'PlaybackUpdateMode = 0, StereoMode = false, Version = "DaVinci Resolve 21.0.3", SavedOutputs = 0, HeldTools = 0, '
    'DisabledTools = 0, LockedTools = 0, AudioOffset = 0, Resumable = true, OutputClips = { }, '
    'CustomData = { TEMPLATE_ID = "Text+" }, Frames = { }, '
    'Prefs = { Comp = { FrameFormat = { GuideRatio = 1.77777777777778, DepthFull = 3, DepthPreview = 3, DepthInteractive = 3, }, } }, '
    'Compressed = true, }'
)

# MixCaps' default look: white Poppins Bold, hard purple (#A855F7) shadow straight to the right, low on screen.
TOOLS = (
    '{ Template = TextPlus { CtrlWZoom = false, Inputs = { '
    'Width = Input { Value = 1920, }, Height = Input { Value = 1080, }, UseFrameFormatSettings = Input { Value = 1, }, '
    'SelectElement = Input { Value = 1, }, Select = Input { Value = 1, }, '
    'StyledText = Input { Value = "MIXCAPS", }, Font = Input { Value = "Poppins", }, Style = Input { Value = "Bold", }, '
    'Size = Input { Value = 0.06, }, VerticalJustificationNew = Input { Value = 3, }, HorizontalJustificationNew = Input { Value = 3, }, '
    'Center = Input { Value = { 0.5, 0.15 }, }, '
    'Red1 = Input { Value = 1, }, Green1 = Input { Value = 1, }, Blue1 = Input { Value = 1, }, '
    'Enabled3 = Input { Value = 1, }, ElementShape3 = Input { Value = 0, }, '
    'Red3 = Input { Value = 0.659, }, Green3 = Input { Value = 0.333, }, Blue3 = Input { Value = 0.969, }, '
    'Alpha3 = Input { Value = 1, }, Opacity3 = Input { Value = 1, }, SoftnessX3 = Input { Value = 0, }, SoftnessY3 = Input { Value = 0, }, '
    'Offset3 = Input { Value = { 0.012, 0 }, } }, '
    'ViewInfo = OperatorInfo { Pos = { 220, 49.5 } }, }, '
    'MediaOut1 = MediaOut { Inputs = { Index = Input { Value = "0", }, Input = Input { SourceOp = "Template", Source = "Output", } }, '
    'ViewInfo = OperatorInfo { Pos = { 440, 49.5 } }, } }'
)


def varint(n):
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def pb_bytes(field, data):
    return bytes([(field << 3) | 2]) + varint(len(data)) + data


def generator_fields(name, seconds=30.0, frames=900):
    inner = pb_bytes(2, name.encode()) + bytes([(6 << 3) | 0]) + varint(4)
    body = (pb_bytes(1, inner) + pb_bytes(2, b"Fusion Title") + pb_bytes(3, b"\x09" + struct.pack("<d", seconds))
            + bytes([(5 << 3) | 0]) + varint(frames) + pb_bytes(6, b"Text+") + pb_bytes(7, b"Text+"))
    payload = b"\x80" + body
    return struct.pack(">II", 2, len(payload)) + payload


def composition_ba(reference_ba):
    """Reuse the Qt map scaffolding (keys/types) from a reference blob, swap in our comp."""
    d = zlib.decompress(reference_ba[4:])
    at = d.index(b"Composition {")
    prefix = d[:at - 4]  # map header + count + 0_name + 0_mediaIdList + "0_data" key/type
    tools = TOOLS.encode()
    data = COMP_HEADER.encode() + b"\x00" + struct.pack("<I", len(tools)) + zlib.compress(tools, 9)
    raw = prefix + struct.pack(">I", len(data)) + data
    return struct.pack(">I", len(raw)) + zlib.compress(raw, 9)


def build(ref_path, out_path):
    z = zipfile.ZipFile(ref_path)
    files = {n: z.read(n).decode("utf-8") for n in z.namelist()}
    sub = [n for n in files if n.count("/") == 3][0]          # MediaPool/Master/000_AutoSubs/MpFolder.xml
    folder_xml = files[sub]

    # keep exactly one generator (the plain Text+ one) and rewrite it as ours
    gens = re.findall(r"  <Element>\s*<Sm2MpGenerator.*?</Sm2MpGenerator>\s*</Element>\n", folder_xml, re.S)
    plain = next(g for g in gens if "<Name>Basic Template</Name>" in g)
    ref_ba = bytes.fromhex(re.search(r"<CompositionBA>([0-9a-fA-F]+)</CompositionBA>", plain).group(1))
    g = plain
    g = re.sub(r"(<Sm2MpGenerator[^>]*>\s*<FieldsBlob>)[0-9a-fA-F]+(</FieldsBlob>)",
               lambda m: m.group(1) + generator_fields(TEMPLATE_NAME).hex() + m.group(2), g)
    g = g.replace("<Name>Basic Template</Name>", f"<Name>{TEMPLATE_NAME}</Name>")
    g = re.sub(r"<CurPlayheadPosition>\d+</CurPlayheadPosition>", "<CurPlayheadPosition>0</CurPlayheadPosition>", g)
    g = re.sub(r"<CompositionBA>[0-9a-fA-F]+</CompositionBA>", "<CompositionBA>" + composition_ba(ref_ba).hex() + "</CompositionBA>", g)
    head = folder_xml[:folder_xml.index("  <Element>")]
    tail = folder_xml[folder_xml.rindex("</Element>\n") + len("</Element>\n"):]
    folder_xml = (head + g + tail).replace("<Name>AutoSubs</Name>", f"<Name>{FOLDER_NAME}</Name>")
    files[sub] = folder_xml
    files["project.xml"] = files["project.xml"].replace("<ProjectName>AutoSubs</ProjectName>", "<ProjectName>MixCaps</ProjectName>")

    # fresh, consistent IDs everywhere
    ids = {}
    pat = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
    def newid(m):
        return ids.setdefault(m.group(0), str(uuid.uuid4()))
    for n in files:
        files[n] = pat.sub(newid, files[n])

    new_sub = sub.replace("000_AutoSubs", "000_" + FOLDER_NAME)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as out:
        for n in ["project.xml", "MediaPool/Master/MpFolder.xml", sub]:
            out.writestr(new_sub if n == sub else n, files[n])
    print("wrote", out_path, "ids replaced:", len(ids))


if __name__ == "__main__":
    build(sys.argv[1], sys.argv[2])
