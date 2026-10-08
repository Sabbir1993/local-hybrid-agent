"""tests/test_tts.py - spoken replies: text preparation, voice choice, WAV packing, graceful absence.

Run: python -m unittest tests.test_tts -v
"""
import struct
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.media import tts
from core.media.constants import MediaError


class CleanForSpeech(unittest.TestCase):
    def test_markdown_links_code_and_emoji_are_not_read_out(self):
        t = tts.clean_for_speech("## Title\n- **bold** item with [a link](http://x.test/a) and `code`.\n"
                                 "```py\nprint(1)\n```\nVisit https://example.com/path now \U0001F600")
        self.assertNotIn("#", t)
        self.assertNotIn("*", t)
        self.assertNotIn("http", t)
        self.assertNotIn("print", t)
        self.assertNotIn("\U0001F600", t)
        self.assertIn("a link", t)
        self.assertIn("code omitted", t)

    def test_a_card_number_is_never_spoken(self):
        t = tts.clean_for_speech("The test card is 4111 1111 1111 1111 on file.")
        self.assertNotIn("4111", t)
        self.assertNotIn("1111", t)
        self.assertIn("a card number was hidden", t)

    def test_think_blocks_are_dropped(self):
        self.assertEqual(tts.clean_for_speech("<think>secret plan</think>Hello there."), "Hello there.")
        self.assertEqual(tts.clean_for_speech("Hi<think>half a thought"), "Hi")


class VoiceChoice(unittest.TestCase):
    def test_script_decides_the_voice(self):
        self.assertEqual(tts.voice_for("Hello, how are you?"), "en")
        self.assertEqual(tts.voice_for("আপনি কেমন আছেন?"), "bn")
        self.assertEqual(tts.voice_for("আপনার balance কত"), "bn")
        self.assertEqual(tts.voice_for("Your আপনার balance is low"), "en")
        self.assertEqual(tts.voice_for("12345"), "en")


class Splitting(unittest.TestCase):
    def test_sentences_including_the_bengali_danda(self):
        self.assertEqual(tts.split_for_tts("One. Two! Three?"), ["One.", "Two!", "Three?"])
        self.assertEqual(tts.split_for_tts("আমি ভালো আছি। আপনি কেমন আছেন?"), ["আমি ভালো আছি।", "আপনি কেমন আছেন?"])

    def test_long_sentence_is_cut_at_a_comma_or_space(self):
        text = ", ".join(f"word{i}" for i in range(120))
        pieces = tts.split_for_tts(text, limit=100)
        self.assertGreater(len(pieces), 1)
        self.assertTrue(all(len(p) <= 100 for p in pieces))
        self.assertEqual(" ".join(pieces).replace(",", "").split(), text.replace(",", "").split())

    def test_pieces_without_words_are_dropped(self):
        self.assertEqual(tts.split_for_tts("... \n ?! \n Real one."), ["Real one."])


class WavPacking(unittest.TestCase):
    def test_header_is_a_valid_mono_pcm16_wav(self):
        pcm = struct.pack("<4h", 0, 1000, -1000, 32767)
        wav = tts.wav_from_pcm(pcm, 24000)
        self.assertEqual(wav[:4], b"RIFF")
        self.assertEqual(wav[8:16], b"WAVEfmt ")
        fmt, ch, rate, byte_rate, align, bits = struct.unpack("<HHIIHH", wav[20:36])
        self.assertEqual((fmt, ch, rate, byte_rate, align, bits), (1, 1, 24000, 48000, 2, 16))
        self.assertEqual(struct.unpack("<I", wav[40:44])[0], len(pcm))
        self.assertEqual(wav[44:], pcm)

    def test_samples_are_clipped_not_wrapped(self):
        self.assertEqual(struct.unpack("<3h", tts._pcm16([2.0, -2.0, 0.0])), (32767, -32767, 0))


class FakeAudio:
    def __init__(self, n=2400, rate=24000):
        self.samples = [0.1] * n
        self.sample_rate = rate


class FakeEngine:
    def __init__(self):
        self.said = []

    def generate(self, piece, sid=0, speed=1.0):
        self.said.append(piece)
        return FakeAudio()


class Synthesis(unittest.TestCase):
    def test_stream_yields_one_chunk_per_piece_and_cleans_the_text_first(self):
        eng = FakeEngine()
        with mock.patch.object(tts, "_engine", return_value=(eng, mock.MagicMock(), 0)):
            chunks = list(tts.synthesize_stream("**Hello** there. Card 4111 1111 1111 1111 ok."))
        self.assertEqual(len(chunks), 2)
        self.assertTrue(all(rate == 24000 for _pcm, rate in chunks))
        self.assertEqual(eng.said[0], "Hello there.")
        self.assertNotIn("4111", " ".join(eng.said))

    def test_wav_reports_voice_and_duration(self):
        eng = FakeEngine()
        with mock.patch.object(tts, "_engine", return_value=(eng, mock.MagicMock(), 0)):
            wav, meta = tts.synthesize_wav("Hello there.")
        self.assertEqual(wav[:4], b"RIFF")
        self.assertEqual(meta["voice"], "en")
        self.assertAlmostEqual(meta["audio_s"], 0.1, places=2)

    def test_nothing_to_say_is_a_plain_error(self):
        with mock.patch.object(tts, "_engine", return_value=(FakeEngine(), mock.MagicMock(), 0)):
            with self.assertRaises(MediaError):
                tts.synthesize_wav("   ")


class MissingPieces(unittest.TestCase):
    def test_without_sherpa_the_error_says_what_to_install(self):
        with mock.patch.object(tts, "_sherpa", return_value=None):
            with self.assertRaises(MediaError) as cm:
                tts._engine("en")
        self.assertIn("sherpa-onnx", str(cm.exception))
        with mock.patch.object(tts, "_sherpa", return_value=None):
            st = tts.status()
        self.assertFalse(st["installed"])
        self.assertFalse(st["ready"])

    def test_missing_voice_folder_names_the_place_to_put_it(self):
        with mock.patch.object(tts, "_sherpa", return_value=object()), \
                mock.patch.object(tts, "_voice_files", return_value=None):
            with self.assertRaises(MediaError) as cm:
                tts._engine("bn")
        self.assertIn("Bangla", str(cm.exception))

    def test_disabled_in_settings(self):
        with mock.patch.object(tts, "tts_cfg", return_value={**tts.tts_cfg(), "enabled": False}):
            with self.assertRaises(MediaError):
                tts._engine("en")


class RouteWiring(unittest.TestCase):
    def test_speak_route_is_authenticated_masks_in_core_and_logs_metadata_only(self):
        src = Path("routes/media.py").read_text(encoding="utf-8")
        i = src.index('@router.post("/media/speak")')
        block = src[i:src.index('@router.post("/media/transcribe")', i)]
        self.assertIn("Depends(get_current_user)", block)
        self.assertIn("media_tts.synthesize_wav", block)
        self.assertIn('action="media.speak"', block)
        self.assertNotIn("req.text", block.split("audit_log(")[1])      # the words never reach the audit log


if __name__ == "__main__":
    unittest.main()
