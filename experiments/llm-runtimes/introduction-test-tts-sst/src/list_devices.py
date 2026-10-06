"""List audio devices so the mic routing can be diagnosed."""

import sounddevice as sd

print("=== OUTPUT devices ===")
for i, d in enumerate(sd.query_devices()):
    if d["max_output_channels"] > 0:
        print(f"  [{i}] {d['name']}  (out={d['max_output_channels']})")

print("=== INPUT devices ===")
for i, d in enumerate(sd.query_devices()):
    if d["max_input_channels"] > 0:
        print(f"  [{i}] {d['name']}  (in={d['max_input_channels']})")

print("=== DEFAULTS ===")
print("  default device:", sd.default.device)
