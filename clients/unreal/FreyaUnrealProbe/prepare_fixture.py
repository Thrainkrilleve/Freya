"""Convert an owner-supplied short PCM WAV into the probe's bounded float fixture."""
import argparse
import array
import hashlib
import json
import sys
import wave
from pathlib import Path

def convert(source: Path, destination: Path):
    with wave.open(str(source), 'rb') as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getcomptype()) != (1, 2, 24000, 'NONE'):
            raise ValueError('Use mono, 24 kHz, 16-bit uncompressed PCM WAV.')
        count = wav.getnframes()
        if not 0 < count <= 30 * 24000:
            raise ValueError('The fixture must be nonempty and at most 30 seconds.')
        pcm = wav.readframes(count)
        if len(pcm) != count * 2:
            raise ValueError('Truncated WAV data.')
    integers = array.array('h', pcm)
    if sys.byteorder != 'little':
        integers.byteswap()
    floats = array.array('f', (sample / 32768.0 for sample in integers))
    if floats.itemsize != 4:
        raise RuntimeError('This Python platform does not use float32 arrays.')
    if sys.byteorder != 'little':
        floats.byteswap()
    # Never overwrite the owner's original or an earlier fixture.
    with destination.open('xb') as output:
        output.write(floats.tobytes())
    return {'sample_rate': 24000, 'channels': 1, 'format': 'float32-le',
            'samples': count, 'seconds': count / 24000,
            'sha256': hashlib.sha256(floats.tobytes()).hexdigest()}

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    print(json.dumps(convert(args.source, args.destination), indent=2))
