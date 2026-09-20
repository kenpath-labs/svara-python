"""``svara.play`` — hear the audio without writing a player.

A quickstart convenience, the same one the ElevenLabs (``play``) and OpenAI
(``LocalAudioPlayer``) SDKs ship. It shells out to ``ffplay`` (part of FFmpeg),
which plays every format the API returns and reads a stream from stdin, so a
``speech.stream()`` starts sounding as the first chunk lands. Nothing here is
imported by the clients; production code should feed its own audio sink.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterable
from typing import Any, List, Optional, Union

from .exceptions import SvaraError

_RAW = {"pcm": "s16le", "ulaw": "mulaw", "alaw": "alaw"}


def _format_of(audio: Any, response_format: Optional[str]) -> Optional[str]:
    if response_format:
        return response_format
    ct = getattr(audio, "content_type", None) or ""
    return {"audio/pcm": "pcm", "audio/basic": "ulaw"}.get(ct.split(";")[0].strip())


def play(
    audio: Union[bytes, Iterable[bytes]],
    *,
    response_format: Optional[str] = None,
    sample_rate: Optional[int] = None,
) -> None:
    """Play ``audio`` — the bytes from ``create()`` or the stream from ``stream()``.

    Container formats (mp3, wav, opus, aac, flac) need nothing else. Raw
    formats are headerless, so the player must be told what they are: a
    ``SpeechResponse``/``SpeechStream`` carries that in its headers; for plain
    bytes pass ``response_format="pcm"`` (and ``sample_rate`` if not 24000).

    Blocks until playback ends. Requires ``ffplay`` on PATH.
    """
    exe = shutil.which("ffplay")
    if exe is None:
        raise SvaraError(
            "svara.play() needs ffplay, which ships with FFmpeg: `brew install ffmpeg`, "
            "`apt install ffmpeg`, or https://ffmpeg.org/download.html. "
            "Or write the audio to a file with .save(path) and open it."
        )
    cmd: List[str] = [exe, "-autoexit", "-nodisp", "-loglevel", "error"]
    is_stream = not isinstance(audio, (bytes, bytearray))
    fmt = None if is_stream else _format_of(audio, response_format)
    if is_stream:
        fmt = response_format  # headers are not known until the first chunk
    proc = None
    try:
        first: Optional[bytes] = None
        it = None
        if is_stream:
            it = iter(audio)  # type: ignore[arg-type]
            first = next(it, b"")
            fmt = fmt or _format_of(audio, None)
        if fmt in _RAW:
            rate = sample_rate or getattr(audio, "sample_rate", None) or 24000
            cmd += ["-f", _RAW[fmt], "-ar", str(rate), "-ch_layout", "mono"]
        cmd += ["-i", "pipe:0"]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        assert proc.stdin is not None
        if is_stream:
            if first:
                proc.stdin.write(first)
            for chunk in it:  # type: ignore[union-attr]
                proc.stdin.write(chunk)
        else:
            proc.stdin.write(bytes(audio))  # type: ignore[arg-type]
        proc.stdin.close()
        proc.wait()
    except BrokenPipeError:
        pass  # the player was closed by the user
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
