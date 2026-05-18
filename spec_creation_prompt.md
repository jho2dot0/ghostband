I'm working on a new song. Help me generate a complete song package.

Artist / project: [artist or project name]
Genre / lane: [e.g. gothic gospel, synth-noir, hip-hop, punk, dream-pop, etc.]
Vibe / topic: [free-form description, can be a vague idea or detailed]
Mood notes: [anything specific — "feel-good," "darker than usual,"
             "first person," "anthemic," etc.]
Length target: [3-3.5 min default unless specified — used by you to
                pick bar counts that add up to roughly that length, but
                NOT included in the spec output]

Give me:

1. Lyrics, with structured section headers
   (verse/prechorus/chorus/bridge/outro).

2. A Suno V5.5 style block, ready to paste. Include vocal direction
   notes if I'll be feeding Suno a voice reference.

3. A Ghostband spec, formatted to be parsed by the Ghostband v0
   script. Use the structured format below — this is the format
   the parser expects. Do NOT include a "Total duration" field
   anywhere in the spec; bar counts are the source of truth and
   the script computes the total itself.

   IMPORTANT: Ghostband renders an INSTRUMENTAL bed. Lead vocals
   live only in the Suno output (artifact 2), never here. The
   Ghostband spec must contain:
   - No lyrics, no lead vocal direction, no spoken word, no
     preaching cadence, no ad-libs, no melismas, no falsetto
     lines, no "[singer] sings/hollers X."
   - No vocal-performance language in References or style tags
     (e.g. drop "Bobby Womack vocal phrasing"; replace with an
     instrumental analogue like "smooth 80s soul guitar/sax
     phrasing"). Reference touchstones should evoke arrangement,
     production, and groove — not singers.
   - No "vocal-forward" or singer-centric Mix notes. Describe the
     instrumental mix (drums, bass, synths, space, saturation).
   - Section Notes should describe the arrangement around where
     a vocal would sit, not what the vocal is doing.

   Wordless vocal TEXTURES are allowed, but only when described
   as instrumental layers — e.g. "distant wordless oohs as a pad
   layer," "airy aah choir texture," "breathy hum underneath the
   Rhodes." Never frame these as a performance, lyric, or lead
   line. If in doubt, leave them out.

GHOSTBAND SPEC FORMAT:

# Title
[song title]

# Artist
[artist or project name]

# Global
Key: [e.g. F# minor]
BPM: [e.g. 76]
Time signature: [e.g. 4/4]
Vibe: [1-3 sentences describing the overall sonic identity]
References: [comma-separated reference touchstones — these will be
             translated into style descriptors, not artist names]
Positive global styles: [comma-separated style tags]
Negative global styles: [comma-separated style tags to exclude]

# Sections
[For each section, in order:]

## [Section name] - [N bars]
Local positive styles: [comma-separated]
Local negative styles: [comma-separated, optional]
Chord progression: [optional, e.g. Am - F - C - G — use ASCII hyphens
                    with spaces, not em-dashes]
Notes: [free-form prose describing the section's character, dynamics,
        instrumentation focus]

[Repeat for all sections.]

# Mix notes
[Any global mix direction — reverb amounts, saturation, stereo image,
 etc. The parser uses these to generate global style tags.]

CRITICAL FORMAT RULES:
- Do NOT include a "Total duration" field — the parser computes total
  from bar counts and BPM
- Use ASCII hyphens (-) with spaces in chord progressions, not em-dashes (—)
- Section headers use the format "## Name - N bars" with an ASCII
  hyphen (the parser also accepts em-dashes here, but hyphens keep
  the spec ASCII-clean)
- Bar counts should add up to roughly the length target I gave above
- INSTRUMENTAL ONLY: no lyrics, no lead vocals, no ad-libs, no spoken
  word, no singer names in References, no "vocal-forward" in Mix notes.
  Wordless vocal textures (oohs/aahs/hums) are allowed only when
  described as instrumental pad/layer elements.

Surprise me if I left anything vague. If the spec needs something the
format doesn't capture, include it in the relevant Notes field.
