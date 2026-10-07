"""Introduction Test (TTS + STT): a terminal interview driven by the orchestrator.

The AI interviews the player to fill a config file, speaking its lines and
listening to the replies. Inference runs in a separate authenticated local
orchestrator service using cached models. Device I/O and config actions stay here.

Usage::

    python main.py                     # text in, speech out (default)
    python main.py --no-tts            # text only, no audio
    python main.py --play --mic        # full voice: speak out, listen on the mic
    python main.py --stt-file reply.wav  # take replies from a WAV instead of typing
    python main.py --out my_config.json
    python main.py --capabilities      # print the probe + planned models and exit
"""

from __future__ import annotations

import argparse
import json
import logging
import queue
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from pathlib import Path
from typing import Any

# The orchestrator lives at the repo root; make it importable when run directly.
# src/ -> introduction-test-tts-sst/ -> llm-runtimes/ -> experiments/ -> repo root
# Add the *package parent* (repo_root/orchestrator), not the repo root itself:
# the repo root contains a folder also called "orchestrator", which would
# otherwise shadow the package as an empty namespace package.
_REPO_ROOT = Path(__file__).resolve().parents[4]
_ORCHESTRATOR_PARENT = _REPO_ROOT / "orchestrator"
if _ORCHESTRATOR_PARENT.is_dir() and str(_ORCHESTRATOR_PARENT) not in sys.path:
    sys.path.insert(0, str(_ORCHESTRATOR_PARENT))

from orchestrator import (
    AudioPlayer,
    AudioRecorder,
    ContinuousRecorder,
    JsonConfigTool,
    ToolRegistry,
    pick_input_device,
    write_wav,
)  # noqa: E402
from orchestrator.errors import OrchestratorError  # noqa: E402
from orchestrator.client import ServiceClient  # noqa: E402

from config_schema import CONFIG_SCHEMA, validate_config  # noqa: E402
from interview import Interview  # noqa: E402

DEFAULT_OUT = Path(__file__).resolve().parent.parent / "out" / "config.json"

def _play_sentence(
    mo: ServiceClient,
    player: AudioPlayer,
    sentence: str,
    out_dir: Path,
    index: int,
    enabled: bool,
) -> None:
    """Synthesize one sentence and queue it for playback immediately.

    Called from the model's streaming callback, so speech starts as soon as the
    first sentence exists rather than after the whole reply is written.
    """
    if not enabled or not sentence.strip():
        return
    try:
        samples, rate = mo.tts.synthesize_samples(sentence)
    except OrchestratorError as exc:
        print(f"  [speech] unavailable: {exc}")
        return
    write_wav(out_dir / f"line_{index:03d}.wav", samples, rate)
    player.play(samples, rate, text=sentence)


def _run_turn(
    interview: Interview,
    mo: ServiceClient,
    player: AudioPlayer,
    recorder: ContinuousRecorder | None,
    out_dir: Path,
    line: int,
    args: argparse.Namespace,
    *,
    opening: bool,
    user_text: str = "",
) -> tuple[Any, bool, int]:
    """Generate one AI turn, speaking it as it streams, watching for barge-in.

    Returns ``(turn, interrupted, next_line_index)``. When the player speaks
    while the AI is talking, playback is stopped and generation is cut off; the
    turn's history keeps only the sentences that were actually spoken, so the
    model's next turn sees exactly what the player heard.
    """
    # Text is already bounded by the service's per-turn token/byte limits.
    # Never backpressure the socket consumer on slow synthesis or playback.
    sentences: queue.Queue[tuple[int, str]] = queue.Queue()
    cancelled = threading.Event()
    generated = threading.Event()
    synthesized = threading.Event()
    results: list[Any] = []
    failures: list[BaseException] = []
    emitted: list[str] = []
    playback_gate = threading.Lock()
    player.begin_turn()

    def on_sentence(sentence: str) -> None:
        if cancelled.is_set():
            return
        index = line + len(emitted)
        emitted.append(sentence)
        sentences.put_nowait((index, sentence))

    def generate() -> None:
        try:
            method = interview.opening_streaming if opening else interview.respond_streaming
            if opening:
                results.append(method(on_sentence, cancelled.is_set))
            else:
                results.append(method(user_text, on_sentence, cancelled.is_set))
        except BaseException as exc:
            failures.append(exc)
            cancelled.set()
        finally:
            generated.set()

    def synthesize() -> None:
        try:
            while not cancelled.is_set():
                try:
                    index, sentence = sentences.get(timeout=0.05)
                except queue.Empty:
                    if generated.is_set():
                        return
                    continue
                print(f"ai>  {sentence}")
                if not args.tts:
                    continue
                if cancelled.is_set():
                    return
                # One shared native worker serializes access to the TTS engine.
                # Cancelling the wait does not terminate native synthesis, but
                # transcription can start immediately and late PCM is ignored.
                future = args.tts_executor.submit(mo.tts.synthesize_samples, sentence)
                while not cancelled.is_set():
                    try:
                        samples, rate = future.result(timeout=0.02)
                        break
                    except FutureTimeout:
                        continue
                else:
                    future.cancel()
                    return
                write_wav(out_dir / f"line_{index:03d}.wav", samples, rate)
                # Synthesize ahead while the previous sentence plays, but bound
                # queued PCM to one sentence so interruption has little backlog.
                while player.playing and not cancelled.wait(0.01):
                    pass
                with playback_gate:
                    if not cancelled.is_set():
                        if args.play and not player.play(samples, rate, text=sentence):
                            raise OrchestratorError(f"audio output failed: {player.error}")
        except BaseException as exc:
            failures.append(exc)
            cancelled.set()
        finally:
            synthesized.set()

    generation_worker = threading.Thread(target=generate, name="voice-generation")
    speech_worker = threading.Thread(target=synthesize, name="voice-synthesis")
    generation_worker.start()
    speech_worker.start()
    interrupted = False
    try:
        while not (generated.is_set() and synthesized.is_set() and not player.playing):
            if recorder is not None and not recorder.available:
                failures.append(OrchestratorError(f"microphone detection failed: {recorder.error}"))
                break
            if recorder is not None and recorder.speech_detected():
                interrupted = True
                cancelled.set()
                with playback_gate:
                    player.abort()
                print("\n  [barge-in] playback stopped; preserving your speech")
                break
            if failures:
                break
            time.sleep(0.01)
    finally:
        if not (generated.is_set() and synthesized.is_set()) or failures:
            cancelled.set()
            with playback_gate:
                player.abort()
        # The orchestration workers exit promptly; an uninterruptible native
        # TTS call stays on the shared executor, with its result discarded.
        generation_worker.join()
        speech_worker.join()
    if failures:
        raise failures[0]
    turn, _ = results[0]
    delivery = player.delivery()
    if interrupted:
        heard: list[str] = []
        for item in delivery:
            if item["heard_samples"] >= item["total_samples"]:
                heard.append(item["text"])
            elif item["heard_samples"]:
                words = item["text"].split()
                fraction = item["heard_samples"] / item["total_samples"]
                count = min(len(words) - 1, int(len(words) * fraction))
                if count:
                    heard.append(" ".join(words[:count]) + "…")
                break
        retained = " ".join(heard)
        turn.say = retained
        interview.interruption_context = (
            "The previous reply was interrupted. Its retained partial-sentence wording "
            "is estimated from played audio; do not assume the player heard later words."
            if retained else "The player interrupted before intelligible reply audio was delivered."
        )
        if retained:
            interview.history[-1]["content"] = retained
        else:
            interview.history.pop()  # No assistant words were delivered.
        print(f"  [history] retained spoken text: {retained!r}")
    else:
        interview.interruption_context = None
    # Persist the distinction between generated, synthesized, and heard text.
    with (out_dir / "delivery.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"turn": interview.turns, "interrupted": interrupted,
                                 "emitted": emitted, "delivery": delivery,
                                 "retained": {"role": "assistant", "content": turn.say},
                                 "private_context": interview.interruption_context},
                                ensure_ascii=False) + "\n")
    return turn, interrupted, line + max(1, len(emitted))


def _report_turn(turn: Any) -> None:
    """Print the tool calls and schema problems a turn produced."""
    for call in turn.tool_calls:
        print(f"  [tool] {call.name}({json.dumps(call.arguments)})")
    if turn.validation_errors:
        print(f"  [schema] {len(turn.validation_errors)} problem(s) reported to the model:")
        for problem in turn.validation_errors:
            print(f"           - {problem}")


def _listen(
    mo: ServiceClient,
    wav: Path | None,
    recorder: ContinuousRecorder | None,
    *,
    capture_dir: Path | None = None,
    capture_index: int = 0,
    interrupted: bool = False,
) -> str:
    """Get the player's reply: from the always-open mic, a WAV, or the keyboard.

    The microphone is opened once at startup and never closed, so an utterance
    spoken while the AI was talking is already queued. The recogniser decides
    where the utterance ends: the offline model waits for a pause, then decodes
    the whole utterance in one pass (far more accurate than the streaming one).

    When ``interrupted`` is set the player has already started speaking, so the
    queued audio is transcribed immediately instead of waiting for a new
    utterance.
    """
    if recorder is not None:
        if interrupted:
            print("  [mic] transcribing your interruption ...")
        else:
            print("  [mic] listening ... (speak, then pause)")
        captured: list[Any] = []
        text = mo.listen_continuous(recorder, on_block=captured.append)
        print(f"you> {text}")
        if capture_dir is not None and captured:
            import numpy as np  # noqa: PLC0415

            capture_dir.mkdir(parents=True, exist_ok=True)
            audio = np.concatenate(captured)
            target = capture_dir / f"utterance_{capture_index:03d}.wav"
            write_wav(target, audio, recorder.sample_rate)
            seconds = len(audio) / recorder.sample_rate
            print(f"  [rec] {target.name}  ({seconds:.1f}s)  transcript={text!r}")
        return text.strip()
    if wav is None:
        try:
            return input("you> ").strip()
        except EOFError:
            return "quit"
    print(f"  [stt] transcribing {wav.name} ...")
    text = mo.transcribe(wav)
    print(f"you> {text}")
    return text.strip()


def run(args: argparse.Namespace) -> int:
    out_dir = args.out.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    print("Probing this machine ...")
    mo = ServiceClient(
        profile="voice" if args.tts or args.mic or args.stt_file else "text",
        url=args.service_url,
        shippable_only=args.shippable_only,
        text_model=args.text_model,
        tts_model=args.tts_model,
        stt_model=args.stt_model,
        speaker_id=args.speaker_id,
    )
    try:
        return _run(args, mo, out_dir)
    finally:
        # Always release the model subprocesses, even on error or Ctrl-C.
        mo.stop()


def _run(args: argparse.Namespace, mo: ServiceClient, out_dir: Path) -> int:
    if args.capabilities:
        print(json.dumps(mo.capabilities(), indent=2))
        return 0

    probe = mo.report
    print(
        f"  tier {probe.tier}  |  "
        f"{probe.total_vram_gb:.1f} GB VRAM ({probe.usable_vram_gb:.1f} GB usable)  |  "
        f"{probe.ram_gb:.1f} GB RAM  |  {probe.free_disk_gb:.1f} GB free disk"
    )

    print("\nCached models loaded by the local service:")
    capabilities = mo.capabilities()
    for kind in ("text", "tts", "stt"):
        required = kind == "text" or (kind == "tts" and args.tts) or (
            kind == "stt" and (args.mic or args.stt_file)
        )
        if required:
            status = capabilities["kinds"][kind]
            if status["state"] != "ready":
                raise OrchestratorError(f"Required service capability {kind} is not ready")
            print(f"  {kind:4} -> {status['model_id']}")

    # The config document is owned by a tool the model can call, not by the
    # model's memory. The interview only ever writes through validated calls.
    config_tool = JsonConfigTool(
        CONFIG_SCHEMA,
        name="config",
        path=args.out,
        description="the player's EchoCradle configuration",
    )
    registry = ToolRegistry()
    config_tool.register_into(registry)

    interview = Interview(mo, config_tool, registry, max_turns=args.max_turns)
    # Audio playback is opt-in: automated runs stay silent, manual runs pass --play.
    player = AudioPlayer(enabled=args.tts and args.play)
    if args.play and not player.available:
        print(f"  (audio output unavailable: {player.error}; continuing silently)")

    # Keep every microphone utterance so a recording can be replayed and
    # compared against its transcript (recording problem vs transcription one).
    capture_dir = out_dir / "captures" if args.capture else None
    if capture_dir is not None:
        print(f"  (saving microphone audio to {capture_dir})")

    # Live microphone input, when asked for. The recorder's sample rate matches
    # whatever the STT model expects, so no resampling is needed.
    recorder = None
    if args.mic:
        # Keep the mic open for the whole session: nothing said while the AI is
        # speaking or thinking is lost. Only safe with headphones.
        #
        # A remote-desktop session exposes the same microphone under several
        # backends and the default one can be silent, so pick the device that
        # actually delivers audio unless the caller pinned one.
        device = args.device
        if device is None:
            device = pick_input_device(sample_rate=mo.stt.sample_rate)
            if device is not None:
                print(f"  (using input device {device})")
        recorder = ContinuousRecorder(
            enabled=True, device=device, sample_rate=mo.stt.sample_rate,
            vad=mo.create_vad(),
        )
        if not recorder.start():
            print(f"  (microphone unavailable: {recorder.error}; falling back to typing)")
            recorder = None
        else:
            # Measure the room so barge-in and endpointing use a threshold that
            # suits this microphone, rather than a fixed guess.
            threshold = recorder.calibrate()
            print(f"  (mic open for the whole session; headphones required)")
            print(f"  (speech threshold calibrated to {threshold:.4f})")
            print("  (Silero VAD confirms speech onset and endpoints; energy alone cannot interrupt)")

    print("\n" + "-" * 68)
    print("The AI will now interview you. Type your replies, or 'quit' to stop.")
    print("-" * 68 + "\n")

    line = 0
    capture_index = 0
    args.tts_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="native-tts")
    try:
        # Opening turn: the AI greets the player. No user input yet.
        turn, interrupted, line = _run_turn(
            interview, mo, player, recorder, out_dir, line, args, opening=True
        )
        _report_turn(turn)

        while interrupted or not (turn.done or interview.complete):
            if interview.turns >= args.max_turns:
                print("(reached the turn limit; finishing with what we have)")
                break

            # Player turn. If they interrupted, their words are already queued;
            # otherwise this waits for them to speak.
            reply = _listen(
                mo, args.stt_file, recorder,
                capture_dir=capture_dir, capture_index=capture_index,
                interrupted=interrupted,
            )
            capture_index += 1
            if _is_stop_command(reply):
                print("(stopped by the player)")
                break
            if not reply:
                print("  [mic] no speech recognized; listening again")
                continue

            # AI turn: generate and speak, watching for the next interruption.
            turn, interrupted, line = _run_turn(
                interview, mo, player, recorder, out_dir, line, args,
                opening=False, user_text=reply,
            )
            _report_turn(turn)
    finally:
        player.close()
        if recorder is not None:
            recorder.stop()
        args.tts_executor.shutdown(wait=True, cancel_futures=True)

    config = interview.config()
    errors = validate_config(config)
    print("\n" + "-" * 68)
    print("Resulting config:")
    print(json.dumps(config, indent=2))

    if errors:
        print("\nValidation problems:")
        for problem in errors:
            print(f"  - {problem}")
        if not args.force:
            print("\nNot writing an invalid config (use --force to write anyway).")
            return 1

    config_tool.data = config
    config_tool.save(args.out)
    print(f"\nWrote {args.out}")
    return 0


def _is_stop_command(text: str) -> bool:
    """Accept recognizer-added punctuation without matching incidental phrases."""
    return re.sub(r"[^\w\s]", "", text).strip().casefold() in {"quit", "exit"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="where to write the config JSON")
    parser.add_argument("--no-tts", dest="tts", action="store_false", help="disable speech synthesis")
    parser.add_argument(
        "--play",
        action="store_true",
        help="play the speech aloud (off by default; for manual testing)",
    )
    parser.add_argument("--stt-file", type=Path, default=None, help="read replies from this WAV via STT")
    parser.add_argument(
        "--mic",
        action="store_true",
        help="take replies from the live microphone, open for the whole session "
        "(needs headphones)",
    )
    parser.add_argument(
        "--capture",
        action="store_true",
        help="save each microphone utterance to out/captures/ for later review",
    )
    parser.add_argument(
        "--device",
        type=int,
        default=None,
        help="pin the microphone input device index (default: auto-pick the "
        "first device that delivers audio)",
    )
    parser.add_argument("--text-model", default=None, help="pin a catalog model id for text")
    parser.add_argument(
        "--tts-model",
        default=None,
        help="pin a catalog model id for speech output (default: orchestrator selection)",
    )
    parser.add_argument(
        "--speaker-id",
        type=int,
        default=None,
        help="override the TTS speaker id (default: selected model's catalog voice)",
    )
    parser.add_argument("--stt-model", default=None, help="pin a catalog model id for speech input")
    parser.add_argument(
        "--service-url", default=None,
        help="attach to a local service; bearer token comes from ECHOCRADLE_SERVICE_TOKEN. "
        "By default launch and own one service process.",
    )
    parser.add_argument("--max-turns", type=int, default=12, help="safety limit on conversation length")
    parser.add_argument("--shippable-only", action="store_true", help="refuse non-commercial weights")
    parser.add_argument("--force", action="store_true", help="write the config even if validation fails")
    parser.add_argument("--capabilities", action="store_true", help="print probe + planned models and exit")
    parser.add_argument("-v", "--verbose", action="store_true", help="log orchestrator decisions")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Models emit typographic characters (non-breaking hyphens, curly quotes)
    # that the Windows console's default cp1252 codec cannot encode. Force
    # UTF-8 on the streams so a stray character cannot crash the run.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        return run(args)
    except KeyboardInterrupt:
        print("\n(interrupted)")
        return 130
    except OrchestratorError as exc:
        print(f"\nCannot run the local service interview: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
