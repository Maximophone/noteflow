"""Is the microphone actually hearing you?

    python -m quickcapture.miccheck

A memo recorded at a level AssemblyAI rejects as "no spoken audio" looks, by
every simple measure, exactly like one that transcribes perfectly: two failed
memos averaged -41 dB and the one that worked -41 dB. Absolute level cannot
separate them, and neither can a spectral check — the failed recordings showed
*more* speech-band energy than the good one.

What does separate them is whether speaking changes anything. So this measures
the room's noise floor, then measures you talking over it, and reports the
difference.
"""

from __future__ import annotations

import argparse
import array
import math
import os
import shutil
import subprocess
import sys
from typing import Optional, Tuple

SAMPLE_RATE = 16000
FULL_SCALE = 32768.0

# A mic that hears you lifts speech well clear of the room. Below ~6 dB of
# difference, whatever reached the file is indistinguishable from the noise that
# was there anyway — which is exactly what a rejected memo looks like.
GOOD_SNR_DB = 15.0
WEAK_SNR_DB = 6.0


def _dbfs(value: float) -> float:
    return 20 * math.log10(value / FULL_SCALE) if value > 0 else -99.0


def _record(ffmpeg: str, mic: str, seconds: float) -> Tuple[Optional[float], Optional[float], str]:
    """Record and return (rms dBFS, peak dBFS, error)."""
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
         "-f", "avfoundation", "-i", mic, "-t", str(seconds),
         "-ar", str(SAMPLE_RATE), "-ac", "1", "-f", "s16le", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if result.returncode != 0 or not result.stdout:
        return None, None, (result.stderr or b"").decode(errors="replace").strip()

    samples = array.array("h")
    samples.frombytes(result.stdout[:len(result.stdout) // 2 * 2])
    if not samples:
        return None, None, "no audio captured"
    rms = math.sqrt(sum(float(v) * v for v in samples) / len(samples))
    return _dbfs(rms), _dbfs(max(abs(v) for v in samples)), ""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m quickcapture.miccheck",
        description="Check whether the microphone can hear you speak.",
    )
    parser.add_argument("--mic", default=os.environ.get("NOTEFLOW_CAPTURE_MIC") or ":default")
    args = parser.parse_args(argv)

    ffmpeg = (os.environ.get("NOTEFLOW_FFMPEG") or shutil.which("ffmpeg")
              or "/opt/homebrew/bin/ffmpeg")
    mic = args.mic if args.mic.startswith(":") else f":{args.mic}"
    print(f"Testing {mic!r}\n")

    print("1/2  Stay quiet for 3 seconds…")
    floor_rms, floor_peak, error = _record(ffmpeg, mic, 3.0)
    if floor_rms is None:
        print(f"     Could not record: {error}")
        return 1
    print(f"     room noise: {floor_rms:.1f} dBFS rms\n")

    input("2/2  Press Return, then say a sentence out loud… ")
    speech_rms, speech_peak, error = _record(ffmpeg, mic, 4.0)
    if speech_rms is None:
        print(f"     Could not record: {error}")
        return 1
    print(f"     while speaking: {speech_rms:.1f} dBFS rms (peak {speech_peak:.1f})\n")

    snr = speech_rms - floor_rms
    print(f"Speaking raised the level by {snr:+.1f} dB")
    if snr >= GOOD_SNR_DB:
        print("VERDICT: the microphone hears you. Memos should transcribe.")
        return 0
    if snr >= WEAK_SNR_DB:
        print("VERDICT: it hears you, but faintly. Quiet words will be lost.\n"
              "         Raise System Settings › Sound › Input volume.")
        return 0
    print("VERDICT: the microphone is not picking up your voice — a memo recorded now\n"
          "         would come back as 'no spoken audio'.\n"
          "         Check: input volume in System Settings › Sound › Input, whether the\n"
          "         lid is closed or something covers the mic, and whether another app\n"
          "         has taken the microphone. If it stays flat at full input volume,\n"
          "         the microphone itself is the problem, not a setting.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
