# Ghostband v0

Single-file CLI that turns a structured song spec into an ElevenLabs-generated MP3.
Claude parses the spec into an ElevenLabs composition plan; ElevenLabs renders the audio.

## Install

```
pip install -r requirements.txt
```

Requires Python 3.11+.

## Usage

```
python ghostband.py specs/example_spec.md
```

Flags:

- `--no-confirm` — skip the parse confirmation prompt
- `--output-dir DIR` — where to write outputs (default: `./outputs`)
- `--seed N` — pass a specific seed to ElevenLabs for reproducibility
- `--dry-run` — parse and display the plan, then stop before calling ElevenLabs

## Credentials

On first run you'll be prompted for your Anthropic and ElevenLabs API keys.
They're stored in your OS keyring under service `ghostband` (usernames
`anthropic` and `elevenlabs`). If a key is rejected with 401 you'll be
re-prompted automatically.

## Output

Each run creates:

```
outputs/<timestamp>_<artist>_<title>/
  audio.mp3
  spec.md          # copy of the input spec
  plan.json        # the parsed ElevenLabs composition plan
  metadata.json    # title, artist, key, BPM, duration, seed, song_id, ...
```

## Writing a spec

You can either hand-write a spec (start from `specs/example_spec.md`) or
have Claude draft one for you using the prompt template in
[`spec_creation_prompt.md`](spec_creation_prompt.md).

To use the prompt:

1. Open `spec_creation_prompt.md` and copy its contents into a Claude chat
   (the web app, Claude Code, or any Claude-powered interface).
2. Fill in the bracketed fields at the top — `Artist / project`,
   `Genre / lane`, `Vibe / topic`, `Mood notes`, `Length target`.
3. Send it. Claude returns three artifacts: lyrics, a Suno V5.5 style
   block (for vocals), and a Ghostband spec (the instrumental bed).
4. Save the Ghostband spec block into `specs/<song>.md` and run
   `python ghostband.py specs/<song>.md`.

The prompt enforces Ghostband's instrumental-only rules (no lead vocals,
no lyrics, no singer references) so the spec is safe to pass straight
to the parser. Lead vocals live in the Suno output, not in the
Ghostband spec.

## Final step: add vocals in Suno

Ghostband only renders the instrumental bed. To finish the song, take the
three artifacts you have in hand and combine them in Suno:

1. **The instrumental MP3** — `outputs/<timestamp>_<artist>_<title>/audio.mp3`
   from your ghostband run.
2. **The Suno V5.5 style block** — artifact #2 from the
   `spec_creation_prompt.md` Claude session.
3. **The lyrics** — artifact #1 from the same Claude session.

In Suno (Custom mode):

1. Use the instrumental MP3 as an audio input — typically "Upload Audio"
   or the "Cover / Extend" flow, depending on which Suno surface you're on.
   This anchors the new generation to your Ghostband arrangement.
2. Paste the Suno style block into the **Style** field.
3. Paste the lyrics into the **Lyrics** field, preserving section headers
   (`[Verse 1]`, `[Chorus]`, etc.) so Suno aligns vocals with your structure.
4. If you have a voice reference (a persona or uploaded vocal), attach it
   here so Suno uses it instead of a generic voice.
5. Generate. Iterate on the style block or lyrics if the result drifts —
   the instrumental and structure stay fixed because the MP3 is anchoring it.

The result is a full track: your Ghostband instrumental with Suno-generated
vocals laid on top.

## Spec format

See `specs/example_spec.md` for a worked example. Headers are `# Title`,
`# Artist`, `# Global`, `# Sections`, `# Mix notes`. Section headers
look like `## Verse 1 - 16 bars` (em dash or hyphen, optional explicit
duration).

### Notes on `# Global` fields

- `Total duration:` — *optional and not authoritative*. The actual song
  duration is computed from the sum of section bar counts and the BPM.
  If this field is present and disagrees with the bar-derived total
  by more than 100ms, ghostband will print a warning. This field can
  be safely omitted from new specs.
