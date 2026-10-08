"""The answer-style rule for a spoken conversation (request field `voice: true`).

The reply is read aloud by the local text-to-speech engine (core/media/tts.py), so it must sound like a person
talking: short sentences, no markup, nothing that only works on a screen.
"""

VOICE_RULES = (
    "\n\n--- VOICE CONVERSATION ---\n"
    "The user is talking to you and your reply is read aloud. Answer the way a person speaks: one to three short "
    "sentences unless more is truly needed, plain words, no markdown, no bullet lists, no tables, no code blocks and "
    "no links. Write numbers, units and dates the way you would say them. Reply in the language the user spoke "
    "(English or Bangla). If something needs the screen (a file, a long list, an approval), say so in one sentence "
    "and point the user to it instead of reading it out. Never read out a card number.\n"
    "--- END VOICE CONVERSATION ---\n")
