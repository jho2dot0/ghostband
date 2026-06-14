"""Ghostband v0 — turn a structured song spec into an ElevenLabs-generated MP3.

Usage:
    python ghostband.py path/to/spec.md [--no-confirm] [--output-dir DIR]
                                        [--seed SEED] [--dry-run]
"""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import json
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import keyring
from anthropic import Anthropic, APIStatusError, AuthenticationError
from elevenlabs import ElevenLabs
from elevenlabs.core.api_error import ApiError as ElevenLabsApiError
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm
from rich.table import Table
from rich.text import Text

KEYRING_SERVICE = "ghostband"
ANTHROPIC_USERNAME = "anthropic"
ELEVENLABS_USERNAME = "elevenlabs"

ANTHROPIC_MODEL = "claude-sonnet-4-6"
ANTHROPIC_MAX_TOKENS = 4096

ELEVENLABS_MODEL_ID = "music_v1"
ELEVENLABS_OUTPUT_FORMAT = "mp3_44100_128"
# Composition-plan only (added 2026-03-16). Honor each section's duration_ms
# rather than relying on the API's shifting default.
ELEVENLABS_RESPECT_SECTIONS_DURATIONS = True

console = Console()


PARSER_SYSTEM_PROMPT = """You are a music arrangement translator. Your job is to convert a structured song spec written in markdown-style prose into a valid ElevenLabs Music API composition plan, formatted as a JSON object.

# Your output

Return ONE JSON object with exactly two top-level keys: `metadata` and `composition_plan`. No markdown fences, no preamble, no explanation. Just the JSON.

```json
{
  "metadata": {
    "title": "string",
    "artist": "string",
    "key": "string",
    "bpm": 0,
    "time_signature": "string",
    "total_duration_ms": 0,
    "total_bars": 0
  },
  "composition_plan": {
    "positive_global_styles": ["string"],
    "negative_global_styles": ["string"],
    "sections": [
      {
        "section_name": "string",
        "positive_local_styles": ["string"],
        "negative_local_styles": ["string"],
        "duration_ms": 0,
        "lines": []
      }
    ]
  }
}
```

# Critical principles

These principles are more important than any other instruction in this prompt. If you have to choose, follow these.

## Principle 1: Style tags are stage directions, not labels

Every entry in `positive_local_styles` and `positive_global_styles` should read like an instruction to a session musician, not a tag in a database. Each tag should have a verb, an instrument, and a role.

WRONG: `"organ"`, `"sparse piano"`, `"tremolo guitar"`, `"brushes"`
RIGHT: `"cathedral organ swells underneath the arrangement"`, `"sparse piano accents on the downbeats"`, `"tremolo guitar fills the gaps between vocal phrases"`, `"brushed drums enter quietly with kick on 1 and 3"`

WRONG: `"climactic"`, `"big reverb"`, `"full band"`
RIGHT: `"the song peaks here at maximum energy"`, `"large hall reverb on the organ and drums"`, `"full band arrangement with all instruments present"`

The verb-phrase form gives the music model way more information than a noun-only tag. Take prose from the spec's Notes fields and convert it directly into verb-phrase tags. Do not summarize prose into terse tags — expand it.

## Principle 2: Negative local styles reserve elements for later sections

Before you write each section's positive and negative styles, look at what instruments and energy levels appear in *later* sections. If an element will appear later, *exclude it from the earlier sections* using `negative_local_styles`.

This is how you build dynamics in a flat-prompt world. The verse intentionally lacks the bridge's distorted guitar and the final chorus's full band, so when those elements arrive, they feel like an arrival.

EXAMPLE: If the Bridge has "distorted tremolo guitar" and the Final Chorus has "full band, big reverb, climactic," then:
- Verse 1 should have negative_local_styles like `"no distorted guitar"`, `"no climactic dynamics yet"`, `"no full band yet"`
- Chorus 1 should have `"no distorted guitar"`, `"no climactic dynamics yet"`
- Bridge should have `"no full band yet, that's reserved for the final chorus"`
- Final Chorus has the climactic full-band elements as positives

Reserved-element negatives are the single most important pattern for producing dynamic arrangements. Do this for every section.

## Principle 3: Preserve dynamics phrases verbatim from the spec

If the spec's Notes field for a section contains energy-level phrases — "restrained throughout," "biggest section of the song," "build to a peak," "drop suddenly," "dynamic lift but still restrained" — those phrases are the song's dynamic arc. Preserve them as their own positive style tags, character-for-character if possible.

Do NOT collapse "restrained throughout" into "subdued dynamics." Keep the spec's exact phrasing. The model honors phrasing the spec author chose.

## Principle 4: Three-layer instrumental enforcement

Always add ALL THREE of these:
- `"instrumental"` in `positive_global_styles`
- `"no vocals"` in `positive_global_styles`
- `"vocals"` and `"singing"` in `negative_global_styles`

Stop there. Do NOT add nine vocal-suppressing tags. If the spec mentions choir, vocal harmonies, or any vocal-adjacent concept, translate it to a non-vocal equivalent in the local positive styles ("synth pad with choral character," "harmonic intensity") rather than adding another negative.

## Principle 5: Honor the spec's specificity

The spec is the user's authorial intent. Be aggressive about preserving it.

- If the spec says "light kick on 1 and 3, snare on 3, brushes preferred over sticks," produce three separate verb-phrase tags, not one summarized one.
- If the spec gives a chord progression, preserve it verbatim as a positive_local_styles entry: `"chord progression: F#m – D – A – E"`.
- HARD RULE — proper-noun scrub: ElevenLabs' content filter rejects any composition plan that names a real musician, band, song, album, label, or trademarked brand. This rule applies to EVERY tag you emit, in EVERY field (positive_global_styles, negative_global_styles, positive_local_styles, negative_local_styles), regardless of where the name appeared in the spec. The spec's References field is the most common source, but proper nouns can also leak in from Vibe, Notes, Mix notes, or anywhere else.

  Forbidden — never emit a tag that contains:
  - An artist or band name in ANY form: bare ("Devo"), adjectival ("Devo-era", "Beatles-y", "Springsteen-style"), possessive ("Bowie's chord changes"), or compound ("Blondie Rapture-style groove").
  - A song or album title ("Rapture", "Kid A", "Songs in the Key of Life") — even in quotes, even hyphenated into a phrase ("Rapture-style groove").
  - A record label name (Motown, Stax, 4AD, etc.).
  - A trademarked brand or product name when used as a style shorthand (NES, Nintendo, Game Boy, Roland TR-808 is OK as a generic instrument but "Roland Juno-60 fat lead" is borderline — prefer "fat analog poly-synth lead"). Game-console names should be generalized: NES → chiptune, Sega Genesis → FM-synth chiptune, Game Boy → 4-bit chiptune.

  Translation procedure — for every proper noun anywhere in the spec:
  1. Identify what the spec is actually pointing at: the era, genre, instrumentation, production style, groove feel, or arrangement convention the artist/song embodies.
  2. Emit 1-2 evocative tags that describe THOSE qualities, using only generic vocabulary (eras, genres, techniques, instrument names, dynamics, textures).
  3. Re-read each tag. If a literal artist/band/song name still appears as a substring, rewrite it.

  Examples:
  - "Low (band)" → "slowcore minimalism with sparse instrumentation and emotional depth"
  - "16 Horsepower" → "dark Americana with raw rootsy feel and haunting atmosphere"
  - "Mark Lanegan" → "gritty deep-voiced vocal style with brooding atmosphere"
  - "Nick Cave and the Bad Seeds" → "gothic post-punk with literary lyricism and theatrical menace"
  - "Vangelis Blade Runner score" → "cinematic synth-noir atmosphere with cold electronic beauty"
  - "Devo-era punk-synth collision" → "angular new-wave punk-synth collision from the early 80s"
  - "Blondie 'Rapture' groove" → "early-80s rap-disco crossover groove with chant-rap feel"
  - "NES boss-fight arpeggio" → "chiptune boss-fight arpeggio with square-wave aggression"

  Self-check before finalizing the plan: scan every tag string for any token that looks like a proper noun (capitalized mid-sentence, in quotes, or hyphenated to "-era"/"-style"/"-esque"). If you find one, rewrite that tag. The user would rather have generic-sounding tags than a rejected plan.
- If the spec's Mix notes mention specific production choices, preserve each as its own tag: `"heavy hall reverb on the organ"`, `"drier mix on guitar and piano"`, `"wide stereo on synth pads"`, `"mono low end"`.

When in doubt, produce MORE tags rather than fewer. ElevenLabs' composition plan can handle many tags per section. The previous parser was producing 5-9 tags per section; you should produce 8-15 verb-phrase tags per section.

## Principle 6: Use ASCII-safe characters in tag strings

Tag strings travel through several encoding layers between you, the script, and the ElevenLabs API. Some non-ASCII characters get corrupted in transit and end up as control characters (e.g. \x1a) in the final request, which the model cannot interpret.

To avoid this, use only ASCII characters in tag strings. Specifically:

- Use a regular hyphen instead of em-dash or en-dash
- Use a straight ASCII apostrophe instead of curly apostrophes
- Use straight ASCII double quotes instead of curly double quotes
- Use three ASCII periods instead of a single ellipsis character

EXAMPLE chord progression tags:
- WRONG: `"chord progression: F#m – D – A – E"` (em-dashes)
- RIGHT: `"chord progression: F#m - D - A - E"` (hyphens)

EXAMPLE descriptive tags:
- WRONG: `"restrained throughout — a first verse, not a climax"` (em-dash)
- RIGHT: `"restrained throughout - a first verse, not a climax"` (hyphen)
- ALSO RIGHT: `"restrained throughout, a first verse not a climax"` (no dash at all)

This is a hard rule. Every tag string you emit must be plain ASCII. The musical content is unchanged; only the punctuation differs.

# Translation rules for the spec format

Specs have these markdown headers: `# Title`, `# Artist`, `# Global`, `# Sections` (with `## SectionName — N bars` subheaders), `# Mix notes`.

## Metadata extraction

- `title`: from `# Title`
- `artist`: from `# Artist`
- `key`, `bpm`, `time_signature`: from the corresponding lines in `# Global`
- `total_bars`: sum of bar counts from all `## Section — N bars` headers
- `total_duration_ms`: ALWAYS computed from total_bars + bpm + time_signature. Use the formula `total_bars * (60 / bpm) * beats_per_bar * 1000`, where beats_per_bar is the numerator of the time signature (e.g. 4 for 4/4, 3 for 3/4). Do NOT use any "Total duration" field from the spec — bar counts are the source of truth.

## Section duration calculation

For each section's `duration_ms`, compute from bar count and BPM using the same formula. The sum of all section duration_ms values must equal total_duration_ms exactly. No proportional rescaling — the bar-derived totals are the truth.

## Global positive styles — what to include

Build positive_global_styles by combining:

1. The Vibe field, broken into its component verb-phrases (1-3 sentences from the spec → 3-6 tags)
2. The References field — for each reference, emit 1-2 style-descriptor tags that capture the artist's sound without naming them. Apply Principle 5's proper-noun scrub to every reference, including adjectival forms like "Devo-era" or "Blondie 'Rapture'-style".
3. Each item from the spec's "Positive global styles" line as its own tag, but rewritten as a verb-phrase if it's a noun-only term
4. Each item from the Mix notes, rewritten as a verb-phrase
5. The three mandatory instrumental tags from Principle 4

Aim for 12-20 entries in positive_global_styles. More is fine.

## Global negative styles — keep minimal

Use only:
1. Each item from the spec's "Negative global styles" line as its own tag
2. The two mandatory vocal-suppression tags (vocals, singing)
3. (Optional) 1-2 "opposite genre" tags if the spec implies them clearly. For example, a slow gospel hymn might have "EDM drops" and "trap hi-hats" as opposite-genre negatives. Be conservative — do not add more than 2 of these.

Cap negative_global_styles at 7 entries total. Fewer is better.

## Section positive styles — be expansive

Build positive_local_styles by combining:

1. Each instrument from the spec's "Local positive styles" line, rewritten as a verb-phrase tag describing what that instrument is doing in this section
2. Each phrase from the section's Notes field, preserved as its own tag (especially dynamics phrases per Principle 3)
3. The chord progression as its own tag if present, in the format `"chord progression: F#m – D – A – E"`
4. Any specific playing techniques from the Notes (e.g. "fingerpicked," "legato," "staccato") as their own tags

Aim for 8-15 entries per section. More is better than fewer.

## Section negative styles — reserve elements

Build negative_local_styles by combining:

1. Each item from the spec's "Local negative styles" line as its own tag
2. RESERVED ELEMENTS per Principle 2: scan all later sections, identify their distinctive instruments and energy levels, and exclude them from this earlier section. This is the most important step.

3-7 entries per section is typical. The final section usually has fewer reserved-element negatives because there are no later sections to reserve elements for.

## Lines field

Always empty array `[]`. Vocals are added later in Suno; the composition plan is instrumental.

# A worked example

Given this section from a spec (BPM 92, with later sections including a Bridge using "tremolo electric guitar, tom-heavy drums" and a Final Chorus with "full band, big reverb tail, climactic"):

```
## Verse 1 - 16 bars
Local positive styles: fingerpicked acoustic, brushed drums, upright bass feel
Chord progression: Am - F - C - G
Notes: Sparse, restrained arrangement. Brushes on snare, kick on 1 and 3. Leave open space in the mid-range for a vocal line to sit on top in post.
```

The correct output for this section is:

```json
{
  "section_name": "Verse 1",
  "positive_local_styles": [
    "fingerpicked acoustic guitar carries the harmonic foundation",
    "brushed snare with kick on 1 and 3",
    "upright bass feel walks gently underneath the changes",
    "sparse and restrained arrangement",
    "open mid-range space reserved for a lead line to sit on top",
    "chord progression: Am - F - C - G",
    "intimate room ambience with minimal reverb"
  ],
  "negative_local_styles": [
    "no tremolo electric guitar",
    "no tom-heavy drums",
    "no climactic dynamics yet",
    "no full band — many instruments held in reserve",
    "no big reverb tail — tighter ambient space here",
    "vocals",
    "singing"
  ],
  "duration_ms": 41739,
  "lines": []
}
```

Notice:
- 7 verb-phrase positive tags expanded from 3 noun tags + chord prog + notes
- "Sparse and restrained" preserved verbatim from the spec, in its own tag
- Reserved-element negatives explicitly call out tremolo electric, tom-heavy drums, full band, climactic dynamics — all elements that appear in later sections
- duration_ms = 16 bars × (60/92) × 4 × 1000 ≈ ~41739 ms

# Final reminders

- Output ONE JSON object with `metadata` and `composition_plan` keys
- No prose, no markdown, no commentary
- Verb-phrase tags throughout, not noun-only tags
- Reserved-element negatives in every section except possibly the last
- Preserve dynamics phrases from the spec verbatim
- Three-layer instrumental enforcement, no more
- Expand the spec, don't summarize it — when in doubt, produce more detail
- Bar counts are the source of truth for all duration math; ignore any "Total duration" field in the spec"""


@dataclass
class ParseResult:
    metadata: dict[str, Any]
    composition_plan: dict[str, Any]


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def _prompt_secret(label: str) -> str:
    console.print(f"[bold]{label}[/bold] (input hidden):")
    value = getpass.getpass("> ").strip()
    if not value:
        console.print("[red]Empty value, aborting.[/red]")
        sys.exit(1)
    return value


def load_credentials(force_reprompt: dict[str, bool] | None = None) -> tuple[str, str]:
    force = force_reprompt or {}

    anthropic_key = None if force.get("anthropic") else keyring.get_password(
        KEYRING_SERVICE, ANTHROPIC_USERNAME
    )
    if not anthropic_key:
        anthropic_key = _prompt_secret("Anthropic API key")
        keyring.set_password(KEYRING_SERVICE, ANTHROPIC_USERNAME, anthropic_key)

    elevenlabs_key = None if force.get("elevenlabs") else keyring.get_password(
        KEYRING_SERVICE, ELEVENLABS_USERNAME
    )
    if not elevenlabs_key:
        elevenlabs_key = _prompt_secret("ElevenLabs API key")
        keyring.set_password(KEYRING_SERVICE, ELEVENLABS_USERNAME, elevenlabs_key)

    return anthropic_key, elevenlabs_key


# ---------------------------------------------------------------------------
# Spec
# ---------------------------------------------------------------------------


def read_spec(spec_path: Path) -> str:
    if not spec_path.exists():
        console.print(f"[red]Spec file not found: {spec_path}[/red]")
        sys.exit(1)
    text = spec_path.read_text(encoding="utf-8")
    # Normalize line endings: a Windows checkout (or CRLF spec) would otherwise
    # carry stray \r control chars into the prose sent to Claude.
    return text.replace("\r\n", "\n").replace("\r", "\n")


# ---------------------------------------------------------------------------
# Parse via Anthropic
# ---------------------------------------------------------------------------


def _strip_json_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    return text.strip()


def parse_spec(spec_text: str, anthropic_key: str) -> ParseResult:
    client = Anthropic(api_key=anthropic_key)
    try:
        message = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=ANTHROPIC_MAX_TOKENS,
            system=PARSER_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": spec_text}],
        )
    except AuthenticationError:
        console.print("[red]Anthropic API key was rejected (401). Re-prompting.[/red]")
        anthropic_key, _ = load_credentials({"anthropic": True})
        return parse_spec(spec_text, anthropic_key)
    except APIStatusError as exc:
        console.print(f"[red]Anthropic API error {exc.status_code}: {exc.message}[/red]")
        sys.exit(1)

    raw = "".join(
        block.text for block in message.content if getattr(block, "type", None) == "text"
    )
    raw = _strip_json_fences(raw)

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        debug_path = Path("ghostband_parser_debug.txt")
        debug_path.write_text(raw, encoding="utf-8")
        console.print(
            f"[red]Claude did not return valid JSON: {exc}. Raw output saved to "
            f"{debug_path}.[/red]"
        )
        sys.exit(1)

    if "metadata" not in payload or "composition_plan" not in payload:
        console.print("[red]Parser output missing 'metadata' or 'composition_plan'.[/red]")
        sys.exit(1)

    return ParseResult(metadata=payload["metadata"], composition_plan=payload["composition_plan"])


# ---------------------------------------------------------------------------
# Post-parse normalization (deterministic; runs before display)
# ---------------------------------------------------------------------------


# Substring triggers (regex, word-boundary-aware) that mark a tag as describing
# a lead-vocal/lyric/performance element. If any of these match AND no safe
# marker matches, the tag is stripped from positive styles before generation.
_VOCAL_TRIGGER_RE = re.compile(
    r"\b("
    r"vocals?|vocally|vocalist|vocalists"          # vocal, vocals, vocal-forward (\bvocal matches), vocalist
    r"|sings?|singing|singer|sung"                 # singing references
    r"|ad[- ]?libs?|ad[- ]?libbing"                # ad-libs / ad libs / adlib
    r"|melismas?|melismatic"                       # melisma / melismatic
    r"|preach|preacher|preaching|preached|sermon"  # preaching cadence
    r"|holler|hollers|hollering"                   # gospel hollers
    r"|lyrics?|lyrical"                            # lyric content
    r"|falsetto"                                   # falsetto lines
    r"|vox"                                        # lead vox / backing vox
    r"|verses?\s+sung|chorus\s+sung"               # explicit sung-section refs
    r"|spoken\s+word|spoken[- ]word"               # spoken word performance
    r")\b",
    re.IGNORECASE,
)

# Wordless-texture markers. If any match the tag, the trigger is overridden
# and the tag is kept. These describe instrumental layers, not performances.
_WORDLESS_SAFE_RE = re.compile(
    r"\b("
    r"wordless"
    r"|oohs?|ooohs?"
    r"|aahs?|ahhs?|aaahs?"
    r"|hum|humming|hummed"
    r"|choir\s+pad|choral\s+pad|choral\s+character|choral\s+quality|choral\s+texture"
    r"|no\s+vocals?|no\s+singing|no\s+lead\s+vocals?"
    r"|without\s+vocals?|without\s+singing"
    r"|instead\s+of\s+vocals?|in\s+place\s+of\s+vocals?"
    r")\b",
    re.IGNORECASE,
)


def is_vocal_track_tag(tag: str) -> bool:
    """Return True if the tag describes a lead-vocal / lyric / vocal-performance
    element that should be stripped from an instrumental plan.

    A tag is flagged when _VOCAL_TRIGGER_RE matches AND _WORDLESS_SAFE_RE does not.
    Wordless textures (oohs, aahs, hums, choir pads) are explicitly preserved.
    The mandatory "no vocals" / "no singing" affirmations are also preserved.
    """
    if not tag or not tag.strip():
        return False
    if not _VOCAL_TRIGGER_RE.search(tag):
        return False
    if _WORDLESS_SAFE_RE.search(tag):
        return False
    return True


def strip_vocal_track_tags(tags: list[str]) -> tuple[list[str], list[str]]:
    """Return (kept_tags, stripped_tags). Only strips actual vocal-track requests."""
    kept: list[str] = []
    stripped: list[str] = []
    for tag in tags:
        if is_vocal_track_tag(tag):
            stripped.append(tag)
        else:
            kept.append(tag)
    return kept, stripped


def strip_vocal_tracks_from_plan(plan: dict[str, Any]) -> list[str]:
    """Walk positive globals and section positive locals, stripping vocal-track requests.

    Returns a list of "<location>: <tag>" strings for any tags that were stripped.
    Negative styles are never modified.
    """
    located: list[str] = []

    pos_g = plan.get("positive_global_styles", []) or []
    kept_g, stripped_g = strip_vocal_track_tags(pos_g)
    plan["positive_global_styles"] = kept_g
    for tag in stripped_g:
        located.append(f"global: {tag}")

    for sec in plan.get("sections", []) or []:
        pos_l = sec.get("positive_local_styles", []) or []
        kept_l, stripped_l = strip_vocal_track_tags(pos_l)
        sec["positive_local_styles"] = kept_l
        name = sec.get("section_name", "?")
        for tag in stripped_l:
            located.append(f"{name}: {tag}")

    return located


_TOTAL_DURATION_LINE = re.compile(
    r"^\s*Total\s+duration\s*:\s*(?P<value>.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def parse_optional_total_duration(spec_text: str) -> int | None:
    """Return milliseconds if `Total duration:` is present in the spec, else None.

    Accepts "M:SS", "MM:SS", or a bare seconds count ("180", "180s", "180.5s").
    Returns None for any unparseable value rather than raising — the field is
    optional and sanity-check-only.
    """
    match = _TOTAL_DURATION_LINE.search(spec_text or "")
    if not match:
        return None
    raw = match.group("value").strip()
    if ":" in raw:
        parts = raw.split(":")
        if len(parts) == 2:
            try:
                minutes = int(parts[0])
                seconds = float(parts[1])
                return int(round((minutes * 60 + seconds) * 1000))
            except ValueError:
                return None
        return None
    cleaned = raw.lower().rstrip("s").strip()
    try:
        return int(round(float(cleaned) * 1000))
    except ValueError:
        return None


def reconcile_durations(plan: dict[str, Any], spec_text: str) -> tuple[int, int | None, int | None]:
    """Bar-derived totals are authoritative; spec's `Total duration` is sanity-check only.

    - Sums section duration_ms to get bar_derived_total_ms.
    - If the spec contains a `Total duration:` line that disagrees by >100ms,
      returns drift_ms so the caller can warn. Sections are NOT modified.
    - Always overwrites metadata.total_duration_ms with the bar-derived sum.

    Returns (bar_derived_total_ms, spec_stated_total_ms, drift_ms_or_None).
    """
    sections = plan.get("sections", []) or []
    bar_derived_total_ms = sum(int(s.get("duration_ms", 0) or 0) for s in sections)

    metadata = plan.setdefault("metadata", {}) if "metadata" in plan else None
    if metadata is not None:
        metadata["total_duration_ms"] = bar_derived_total_ms

    spec_stated_total_ms = parse_optional_total_duration(spec_text)
    drift: int | None = None
    if spec_stated_total_ms is not None:
        drift = bar_derived_total_ms - spec_stated_total_ms

    return bar_derived_total_ms, spec_stated_total_ms, drift


# ---------------------------------------------------------------------------
# Confirm
# ---------------------------------------------------------------------------


def _bars_for_section(duration_ms: int, bpm: int, beats_per_bar: int) -> float:
    if bpm <= 0 or beats_per_bar <= 0:
        return 0.0
    return duration_ms / 1000.0 / (60.0 / bpm) / beats_per_bar


def _beats_per_bar(time_sig: str) -> int:
    m = re.match(r"\s*(\d+)\s*/\s*\d+\s*", time_sig or "")
    return int(m.group(1)) if m else 4


def display_plan(result: ParseResult) -> None:
    md = result.metadata
    plan = result.composition_plan

    duration_s = md.get("total_duration_ms", 0) / 1000.0
    duration_str = f"{int(duration_s) // 60}:{int(duration_s) % 60:02d}"

    metadata_text = Text()
    metadata_text.append(f"Title: ", style="bold")
    metadata_text.append(f"{md.get('title', '?')}\n")
    metadata_text.append(f"Artist: ", style="bold")
    metadata_text.append(f"{md.get('artist', '?')}\n")
    metadata_text.append(f"Key: ", style="bold")
    metadata_text.append(f"{md.get('key', '?')}    ")
    metadata_text.append(f"BPM: ", style="bold")
    metadata_text.append(f"{md.get('bpm', '?')}    ")
    metadata_text.append(f"Time: ", style="bold")
    metadata_text.append(f"{md.get('time_signature', '?')}    ")
    metadata_text.append(f"Duration: ", style="bold")
    metadata_text.append(f"{duration_str} ({int(duration_s)}s)")
    console.print(Panel(metadata_text, title="Metadata", border_style="cyan"))

    styles_text = Text()
    styles_text.append("Positive:\n", style="bold green")
    for tag in plan.get("positive_global_styles", []):
        styles_text.append(f"  • {tag}\n", style="green")
    styles_text.append("\nNegative:\n", style="bold red")
    for tag in plan.get("negative_global_styles", []) or ["(none)"]:
        styles_text.append(f"  • {tag}\n", style="red")
    console.print(Panel(styles_text, title="Global styles", border_style="cyan"))

    bpm = md.get("bpm", 0)
    bpb = _beats_per_bar(md.get("time_signature", "4/4"))

    table = Table(title="Sections", border_style="cyan", show_lines=False)
    table.add_column("Name", style="bold")
    table.add_column("Bars", justify="right")
    table.add_column("Seconds", justify="right")
    table.add_column("Positive", style="green")
    table.add_column("Negative", style="red")

    total_section_ms = 0
    for sec in plan.get("sections", []):
        d_ms = sec.get("duration_ms", 0)
        total_section_ms += d_ms
        bars = _bars_for_section(d_ms, bpm, bpb)
        table.add_row(
            sec.get("section_name", "?"),
            f"{bars:.1f}",
            f"{d_ms / 1000:.1f}",
            ", ".join(sec.get("positive_local_styles", []) or []),
            ", ".join(sec.get("negative_local_styles", []) or []) or "—",
        )
    console.print(table)

    drift_ms = total_section_ms - md.get("total_duration_ms", 0)
    summary = (
        f"Total of section durations: {total_section_ms / 1000:.2f}s "
        f"(spec total: {md.get('total_duration_ms', 0) / 1000:.2f}s, drift {drift_ms:+d}ms)\n"
        f"Estimated cost: depends on tier — generation will use ~{int(duration_s)}s of audio."
    )
    console.print(Panel(summary, border_style="yellow"))


def confirm_plan() -> bool:
    return Confirm.ask("Generate audio?", default=False)


# ---------------------------------------------------------------------------
# Generate
# ---------------------------------------------------------------------------


def _collect_audio_bytes(audio: Any) -> bytes:
    if isinstance(audio, (bytes, bytearray)):
        return bytes(audio)
    chunks: list[bytes] = []
    for chunk in audio:
        if chunk:
            chunks.append(chunk)
    return b"".join(chunks)


def generate_audio(
    plan: dict[str, Any],
    elevenlabs_key: str,
    seed: int | None,
    allow_suggestion_retry: bool = True,
) -> tuple[bytes, str | None, dict[str, Any]]:
    client = ElevenLabs(api_key=elevenlabs_key)

    kwargs: dict[str, Any] = {
        "composition_plan": plan,
        "output_format": ELEVENLABS_OUTPUT_FORMAT,
        "model_id": ELEVENLABS_MODEL_ID,
        "respect_sections_durations": ELEVENLABS_RESPECT_SECTIONS_DURATIONS,
    }
    if seed is not None:
        kwargs["seed"] = seed

    try:
        with client.music.with_raw_response.compose(**kwargs) as raw_response:
            audio_bytes = _collect_audio_bytes(raw_response.data)
            song_id = None
            headers = getattr(raw_response, "headers", None) or getattr(
                getattr(raw_response, "_response", None), "headers", None
            )
            if headers:
                for key in ("song_id", "song-id", "x-song-id", "elevenlabs-song-id"):
                    if key in headers:
                        song_id = headers[key]
                        break
        return audio_bytes, song_id, plan
    except AttributeError:
        audio = client.music.compose(**kwargs)
        return _collect_audio_bytes(audio), None, plan
    except ElevenLabsApiError as exc:
        status = getattr(exc, "status_code", None)
        body = getattr(exc, "body", None) or {}
        message = (body or {}).get("detail") if isinstance(body, dict) else str(body)
        if status == 401:
            console.print("[red]ElevenLabs API key was rejected (401). Re-prompting.[/red]")
            _, fresh_key = load_credentials({"elevenlabs": True})
            return generate_audio(plan, fresh_key, seed)
        if status == 402 or (isinstance(message, str) and "credit" in message.lower()):
            console.print(f"[red]ElevenLabs: insufficient credits — {message}[/red]")
            sys.exit(2)
        if status == 429:
            retry_after = ""
            try:
                retry_after = exc.headers.get("retry-after", "")  # type: ignore[attr-defined]
            except Exception:
                pass
            console.print(f"[red]ElevenLabs rate limit. Retry after: {retry_after}[/red]")
            sys.exit(3)
        suggestion = None
        if isinstance(body, dict):
            suggestion = (body.get("data") or {}).get("composition_plan_suggestion")
        if (
            allow_suggestion_retry
            and suggestion
            and isinstance(suggestion, dict)
            and "sections" in suggestion
        ):
            console.print(
                f"[yellow]ElevenLabs rejected the plan ({message}). They returned a "
                f"sanitized suggestion.[/yellow]"
            )
            console.print(
                Panel(
                    json.dumps(suggestion, indent=2),
                    title="composition_plan_suggestion",
                    border_style="yellow",
                )
            )
            if Confirm.ask("Retry with ElevenLabs' suggested plan?", default=True):
                return generate_audio(
                    suggestion, elevenlabs_key, seed, allow_suggestion_retry=False
                )
        debug_path = Path("ghostband_plan_debug.json")
        debug_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
        console.print(
            f"[red]ElevenLabs error {status}: {message}\nPlan dumped to {debug_path}.[/red]"
        )
        sys.exit(4)


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


def _slug(value: str) -> str:
    value = value.lower().replace(" ", "_")
    value = re.sub(r"[^a-z0-9_]+", "", value)
    return value or "untitled"


def write_outputs(
    output_dir: Path,
    spec_path: Path,
    spec_text: str,
    parsed: ParseResult,
    audio_bytes: bytes,
    song_id: str | None,
    seed: int | None,
) -> tuple[Path, str]:
    timestamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    folder_name = f"{timestamp}_{_slug(parsed.metadata.get('artist', ''))}_{_slug(parsed.metadata.get('title', ''))}"
    target = output_dir / folder_name
    target.mkdir(parents=True, exist_ok=True)

    audio_filename = f"Ghostband-{_slug(parsed.metadata.get('title', ''))}.mp3"
    (target / audio_filename).write_bytes(audio_bytes)
    shutil.copyfile(spec_path, target / "spec.md")
    (target / "plan.json").write_text(
        json.dumps(parsed.composition_plan, indent=2), encoding="utf-8"
    )

    metadata_blob = {
        **parsed.metadata,
        "seed": seed,
        "song_id": song_id,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "elevenlabs_model_id": ELEVENLABS_MODEL_ID,
        "elevenlabs_output_format": ELEVENLABS_OUTPUT_FORMAT,
        "anthropic_model": ANTHROPIC_MODEL,
        "audio_bytes": len(audio_bytes),
    }
    (target / "metadata.json").write_text(json.dumps(metadata_blob, indent=2), encoding="utf-8")

    return target, audio_filename


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Ghostband v0 — spec to mp3.")
    parser.add_argument("spec_file", type=Path)
    parser.add_argument("--no-confirm", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=Path("./outputs"))
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    anthropic_key, elevenlabs_key = load_credentials()

    spec_text = read_spec(args.spec_file)
    console.print(f"[dim]Parsing spec via Anthropic ({ANTHROPIC_MODEL})…[/dim]")
    parsed = parse_spec(spec_text, anthropic_key)

    stripped_tags = strip_vocal_tracks_from_plan(parsed.composition_plan)
    if stripped_tags:
        warning = Text()
        warning.append(
            f"{len(stripped_tags)} positive style tag(s) referenced lead vocals, "
            f"lyrics, or vocal performances and were removed before generation. "
            f"Ghostband renders an instrumental bed; lead vocals belong in the "
            f"Suno pass. Wordless oohs/aahs/hums and choir-pad textures are kept.\n\n",
            style="yellow",
        )
        warning.append("Stripped tags:\n", style="bold yellow")
        for loc in stripped_tags:
            warning.append(f"  • {loc}\n", style="yellow")
        warning.append(
            "\nTo silence this: rewrite the spec so positive styles describe "
            "instrumentation only. Vocal textures must be framed as instrumental "
            "layers (e.g. \"wordless ooh pad,\" \"choir-pad synth\").",
            style="dim yellow",
        )
        console.print(
            Panel(
                warning,
                title="⚠  Vocal-tag filter tripped",
                border_style="yellow",
            )
        )

    bar_total_ms, spec_total_ms, drift = reconcile_durations(
        {"metadata": parsed.metadata, "sections": parsed.composition_plan.get("sections", [])},
        spec_text,
    )
    if drift is not None and abs(drift) > 100:
        console.print(
            f"[yellow]WARNING:[/yellow] spec's 'Total duration' of "
            f"{(spec_total_ms or 0) / 1000:.1f}s disagrees with bar-derived total of "
            f"{bar_total_ms / 1000:.1f}s (drift {drift / 1000:+.1f}s). Using bar-derived "
            f"total. Remove or correct 'Total duration:' in the spec to silence this warning."
        )

    display_plan(parsed)

    if args.dry_run:
        console.print("[yellow]--dry-run set, stopping before ElevenLabs call.[/yellow]")
        return

    if not args.no_confirm and not confirm_plan():
        console.print("Aborted.")
        return

    console.print(f"[dim]Generating audio via ElevenLabs ({ELEVENLABS_MODEL_ID})…[/dim]")
    audio_bytes, song_id, used_plan = generate_audio(
        parsed.composition_plan, elevenlabs_key, args.seed
    )
    parsed.composition_plan = used_plan

    target, audio_filename = write_outputs(
        args.output_dir, args.spec_file, spec_text, parsed, audio_bytes, song_id, args.seed
    )

    audio_mb = len(audio_bytes) / (1024 * 1024)
    duration_s = parsed.metadata.get("total_duration_ms", 0) / 1000.0
    console.print()
    console.print(f"[green]✓ Generated:[/green] {target}/")
    console.print(f"  Audio: {audio_filename} ({audio_mb:.1f} MB, {int(duration_s)}s)")
    console.print(f"  Seed: {args.seed if args.seed is not None else '(server-assigned)'}")
    console.print(f"  Song ID: {song_id or '(not returned)'}")
    console.print()
    console.print(f"Next: upload {audio_filename} to Suno as a cover with your style block and lyrics.")


if __name__ == "__main__":
    main()
