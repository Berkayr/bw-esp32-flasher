# Firmware-family dispatch for raw .bin files, matching ScooterTeam 8baeea1.
# CC BY-NC-SA 4.0; see LICENSE.
import bwflash


def detect_firmware_type(path):
    try:
        with open(path, 'rb') as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(0x800)
            signature = f.read(8)
            f.seek(0)
            header = f.read(16)
        # Upstream tries Brightway first, including images carrying both signatures.
        if size >= 0x1000:
            if signature == b'DEPRD5C\x00':
                return 'Brightway'
            offsets = bwflash._find_all(path, b'\x63\x7c')
            if len(offsets) == 1 and offsets[0] > 0x1000:
                return 'Brightway'
        import leqiflash
        if leqiflash.header_size(header, size) is not None:
            return 'LEQI'
        return 'Unknown'
    except OSError as exc:
        raise bwflash.FlashError('preflight: cannot read image; check file path and filesystem: ' + str(exc))


def flash(path, debug=False, progress=None):
    family = detect_firmware_type(path)
    if family == 'Brightway':
        return bwflash.flash(path, debug=debug, progress=progress)
    if family == 'LEQI':
        import leqiflash
        return leqiflash.flash(path, debug=debug, progress=progress)
    raise bwflash.FlashError('preflight: unknown firmware family; select a complete raw Brightway or LEQI .bin')
