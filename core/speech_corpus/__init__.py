"""core/speech_corpus - colleagues read sentences aloud on /voice; the clips become a Bangla speech corpus.

The corpus lives in its own folder outside the app (speech_corpus.dir in app.json) with its own SQLite file, so it can
be copied to the training machine as a whole. Training is a separate application: this package only collects, checks
and stores. The schema in store.py is what the trainer reads.

  settings.py  config block, folder validation, enable switch, access code
  audio.py     strict WAV checks (16 kHz mono 16-bit PCM, length, silence, clipping)
  store.py     corpus.db schema and every read/write, speaker folders
  prompts.py   prompt import (with the card-number check) and per-session selection
"""
