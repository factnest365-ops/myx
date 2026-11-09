"""Audiophile DSP, Parametric EQ, Loudness Normalization & Crossfade Engine for MYX.

Compiles high-level DSP presets, custom 5-band EQ, EBU/dynamic loudness
normalization, silence trimming, and speed/pitch profiles into mpv IPC
properties (`af`, `speed`, `audio-pitch-correction`) with zero external deps.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

EQ_FREQS_HZ: tuple[int, ...] = (60, 230, 910, 3600, 14000)

DSP_PRESETS: dict[str, dict[str, Any]] = {
    "flat": {
        "label": "Flat (Bit-Perfect)",
        "filters": [],
        "speed": 1.0,
        "pitch_correction": True,
    },
    "bass_boost": {
        "label": "Bass Boost",
        "filters": ["bass=g=6.0:f=95:w=0.6"],
        "speed": 1.0,
        "pitch_correction": True,
    },
    "vocal": {
        "label": "Vocal Presence",
        "filters": [
            "equalizer=f=220:t=q:w=1.0:g=-2.0",
            "equalizer=f=2600:t=q:w=1.2:g=4.5",
            "equalizer=f=5200:t=q:w=1.0:g=2.0",
        ],
        "speed": 1.0,
        "pitch_correction": True,
    },
    "treble_sparkle": {
        "label": "Treble Sparkle",
        "filters": ["treble=g=5.0:f=4500:w=0.6"],
        "speed": 1.0,
        "pitch_correction": True,
    },
    "rock": {
        "label": "Rock / Punch",
        "filters": [
            "bass=g=4.5:f=110:w=0.6",
            "equalizer=f=1000:t=q:w=1.0:g=-1.5",
            "treble=g=4.0:f=4200:w=0.6",
        ],
        "speed": 1.0,
        "pitch_correction": True,
    },
    "electronic": {
        "label": "Electronic / Club",
        "filters": [
            "bass=g=6.5:f=75:w=0.5",
            "treble=g=4.5:f=6000:w=0.6",
        ],
        "speed": 1.0,
        "pitch_correction": True,
    },
    "late_night": {
        "label": "Late Night Compressor",
        "filters": ["acompressor=threshold=-18dB:ratio=3:attack=15:release=250:makeup=3"],
        "speed": 1.0,
        "pitch_correction": True,
    },
    "spatial": {
        "label": "Spatial Wide",
        "filters": ["extrastereo=m=1.45"],
        "speed": 1.0,
        "pitch_correction": True,
    },
    "slowed_reverb": {
        "label": "Slowed + Reverb",
        "filters": ["aecho=0.8:0.88:55:0.35"],
        "speed": 0.86,
        "pitch_correction": False,
    },
    "nightcore": {
        "label": "Nightcore",
        "filters": ["bass=g=2.5:f=100:w=0.6"],
        "speed": 1.20,
        "pitch_correction": False,
    },
}

PRESET_ORDER: tuple[str, ...] = tuple(DSP_PRESETS.keys())


def _clamp_gain(val: Any) -> float:
    try:
        f = float(val)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(f):
        return 0.0
    return round(max(-12.0, min(12.0, f)), 2)


@dataclass
class DSPConfig:
    preset: str = "flat"
    bands_db: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0, 0.0, 0.0])
    normalize: bool = False
    trim_silence: bool = False
    crossfade_s: float = 0.0

    def cycle_preset(self, step: int = 1) -> str:
        idx = PRESET_ORDER.index(self.preset) if self.preset in PRESET_ORDER else 0
        self.preset = PRESET_ORDER[(idx + int(step)) % len(PRESET_ORDER)]
        return self.preset

    def set_preset(self, name: str) -> bool:
        key = str(name or "").strip().lower().replace("-", "_").replace(" ", "_")
        if key not in DSP_PRESETS:
            return False
        self.preset = key
        return True

    @property
    def custom_bands(self) -> list[float]:
        return list(self.bands_db)

    def set_bands(self, gains: list[Any] | tuple[Any, ...]) -> list[float]:
        padded = (list(gains)[:5] + [0.0] * 5)[:5]
        self.bands_db = [_clamp_gain(g) for g in padded]
        return list(self.bands_db)

    def set_custom_bands(self, gains: list[Any] | tuple[Any, ...]) -> list[float]:
        self.preset = "custom"
        return self.set_bands(gains)

    def set_crossfade(self, seconds: Any) -> float:
        try:
            f = float(seconds)
        except (TypeError, ValueError):
            f = 0.0
        if not math.isfinite(f):
            f = 0.0
        self.crossfade_s = round(max(0.0, min(12.0, f)), 1)
        return self.crossfade_s

    def to_dict(self) -> dict[str, Any]:
        preset_info = DSP_PRESETS.get(self.preset, DSP_PRESETS["flat"])
        label = (
            "Custom 5-Band EQ"
            if self.preset == "custom"
            else preset_info["label"]
        )
        return {
            "preset": self.preset,
            "preset_label": label,
            "bands_db": list(self.bands_db),
            "normalize": self.normalize,
            "trim_silence": self.trim_silence,
            "crossfade_s": self.crossfade_s,
            "speed": preset_info["speed"],
        }


def build_lavfi_chain(cfg: DSPConfig) -> list[str]:
    """Return ordered ffmpeg/libavfilter expressions for the active DSPConfig."""
    chain: list[str] = []
    if cfg.trim_silence:
        chain.append(
            "silenceremove=start_periods=1:start_duration=0.15:start_threshold=-52dB"
        )
    preset_info = DSP_PRESETS.get(cfg.preset, DSP_PRESETS["flat"])
    chain.extend(preset_info.get("filters", []))
    for freq, gain in zip(EQ_FREQS_HZ, cfg.bands_db):
        g = _clamp_gain(gain)
        if abs(g) >= 0.05:
            chain.append(f"equalizer=f={freq}:t=q:w=1.1:g={g}")
    if cfg.normalize:
        chain.append("dynaudnorm=f=150:g=13:p=0.92")
    return chain


def build_mpv_af(cfg: DSPConfig) -> str:
    """Compile DSPConfig into an mpv `af` property string (empty string if flat)."""
    chain = build_lavfi_chain(cfg)
    if not chain:
        return ""
    return f"lavfi=[{','.join(chain)}]"


def mpv_dsp_commands(cfg: DSPConfig) -> list[list[Any]]:
    """Return the sequence of mpv IPC commands needed to apply `cfg`."""
    preset_info = DSP_PRESETS.get(cfg.preset, DSP_PRESETS["flat"])
    af_str = build_mpv_af(cfg)
    speed = float(preset_info.get("speed", 1.0))
    pitch_corr = "yes" if preset_info.get("pitch_correction", True) else "no"
    return [
        ["set_property", "af", af_str],
        ["set_property", "audio-pitch-correction", pitch_corr],
        ["set_property", "speed", speed],
    ]


def compute_crossfade_gain(
    position: float | None, duration: float | None, crossfade_s: float
) -> float:
    """Compute [0.15 .. 1.0] volume envelope multiplier during crossfade ramps."""
    if crossfade_s <= 0 or position is None or duration is None:
        return 1.0
    if duration <= crossfade_s * 2.2 or position < 0:
        return 1.0
    # Fade-in over first min(crossfade_s * 0.6, 2.5s)
    fade_in_len = min(crossfade_s * 0.6, 2.5)
    if 0.0 <= position < fade_in_len and fade_in_len > 0:
        return round(max(0.2, min(1.0, 0.2 + 0.8 * (position / fade_in_len))), 3)
    # Fade-out over final crossfade_s seconds
    remaining = duration - position
    if 0.0 <= remaining <= crossfade_s:
        return round(max(0.15, min(1.0, remaining / crossfade_s)), 3)
    return 1.0
