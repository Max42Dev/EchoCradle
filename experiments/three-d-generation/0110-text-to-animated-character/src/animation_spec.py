"""Validate bounded, data-driven local motion requests before starting workers."""
from __future__ import annotations

import re


def validate_clips(document: dict) -> list[dict]:
    clips = document.get("clips")
    if not isinstance(clips, list) or not 1 <= len(clips) <= 5:
        raise ValueError("Supply one to five animation clips")
    names = set()
    for clip in clips:
        if not isinstance(clip, dict):
            raise ValueError("Animation must be an object")
        name = clip.get("id")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name):
            raise ValueError("Invalid animation ID")
        if name.casefold() in names:
            raise ValueError("Duplicate animation ID")
        names.add(name.casefold())
        if not isinstance(clip.get("prompt"), str) or not 1 <= len(clip["prompt"]) <= 1000:
            raise ValueError("Invalid motion prompt")
        frames = clip.get("frames")
        if type(frames) is not int or not 4 <= frames <= 196 or frames % 4:
            raise ValueError("Motion frames must be 4..196 and divisible by four")
        if type(clip.get("fps")) is not int or clip["fps"] != 20:
            raise ValueError("Motion fps must be 20")
        if type(clip.get("seed")) is not int or not 0 <= clip["seed"] < 2 ** 32:
            raise ValueError("Invalid motion seed")
        if clip.get("root_motion") not in ("in_place", "travel"):
            raise ValueError("Invalid root motion policy")
        if type(clip.get("loop")) is not bool:
            raise ValueError("Loop intent must be boolean")
    return clips